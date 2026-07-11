"""Client-side code-agent parallelism (engine/client.py).

The thin client runs ``claude --print`` for staged handovers in a bounded pool. These
tests validate the pool semantics WITHOUT launching claude (the handover runner is
stubbed): real concurrency (N agents run at once), claim-once (no double launch across
polls), and unclaim-on-failure (a failed task is retried next poll).
"""
from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

# core/engine on sys.path so `import client` works (conftest adds core/). The client
# is pure stdlib (no gx10 / openai import), so nothing heavy needs stubbing.
_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import client  # noqa: E402
import pytest  # noqa: E402


class _FakeServer:
    def __init__(self, pending):
        self._pending = pending
        self.uploaded = []
        self._lock = threading.Lock()

    def pending(self):
        return list(self._pending)

    def feedback(self, task_id, agent, content, exit_code=None, stderr=""):   # #455: accept the run signal
        with self._lock:
            self.uploaded.append((task_id, agent, content))
        # Deliberately OMITS "classification" → exercises _process_one's `cls is None` back-compat
        # branch (an older server that doesn't classify): feedback present ⇒ success; none ⇒ unclaim.
        return {"feedback_file": f".ironclad/agent/feedback/{task_id}_{agent}-feedback.md"}


def _items(*ids):
    return [{"id": t, "agent": "OPUS", "title": t, "type": "feature"} for t in ids]


def test_build_argv_default_is_claude_shape():
    argv = client.build_agent_argv(
        client.DEFAULT_AGENT_CMD, bin="claude", model="m", effort="high",
        permission="acceptEdits", prompt="do the thing with spaces")
    assert argv == ["claude", "--model", "m", "--effort", "high",
                    "--permission-mode", "acceptEdits", "--print",
                    "do the thing with spaces"]   # prompt stays ONE arg


def test_build_argv_prompt_single_arg_any_template():
    argv = client.build_agent_argv(
        "mytool --yes {prompt}", bin="x", model="x", effort="x",
        permission="x", prompt="a b c")
    assert argv == ["mytool", "--yes", "a b c"]    # CLI with no model/effort flags


def test_build_argv_embedded_placeholder():
    argv = client.build_agent_argv(
        "tool --model={model} {prompt}", bin="b", model="kimi-x", effort="e",
        permission="p", prompt="hi there")
    assert argv == ["tool", "--model=kimi-x", "hi there"]


def test_strip_confirm_any_position():
    # #1281: `--yes`/`--confirm` is the destructive-command confirmation in ANY position, not only trailing —
    # `--yes --purge` (a flag after --yes) must be recognised too.
    assert client._strip_confirm("project delete X --purge --yes") == ("project delete X --purge", True)
    assert client._strip_confirm("project delete X --yes --purge") == ("project delete X --purge", True)
    assert client._strip_confirm("project delete X --confirm") == ("project delete X", True)
    assert client._strip_confirm("project delete X --purge") == ("project delete X --purge", False)
    assert client._strip_confirm("normal chat") == ("normal chat", False)


def test_build_argv_codex_template_drops_claude_only_flags():
    # #442 (epic #440 Phase 2): the template-driven client lane runs Codex with ZERO core change.
    # `codex exec` REJECTS --effort/--permission-mode/-a (verified live, §C0R-8); a Codex cmd_template
    # therefore omits {effort}/{permission}, and the builder must NOT leak the Claude-only flags/values.
    tmpl = "{bin} exec -m {model} -s workspace-write -c 'approval_policy=\"never\"' --skip-git-repo-check {prompt}"
    argv = client.build_agent_argv(
        tmpl, bin="codex", model="gpt-5.5", effort="high", permission="acceptEdits",
        prompt="analyze the repo and write feedback")
    assert argv == ["codex", "exec", "-m", "gpt-5.5", "-s", "workspace-write",
                    "-c", 'approval_policy="never"', "--skip-git-repo-check",
                    "analyze the repo and write feedback"]
    # the Claude-only flags AND their values never leak into the Codex argv
    assert "--effort" not in argv and "--permission-mode" not in argv and "-a" not in argv
    assert "high" not in argv and "acceptEdits" not in argv
    assert argv[-1] == "analyze the repo and write feedback"   # prompt stays ONE arg


def test_run_handover_passes_permission_mode(tmp_path, monkeypatch):
    """Regression: the headless code-agent MUST get a non-interactive permission mode,
    else claude --print can't write files (it silently exits without doing the work)."""
    captured = {}

    class _R:
        returncode = 0

    def _fake_run(argv, **kw):
        captured["argv"] = argv
        # simulate claude writing the expected feedback file so the runner returns it
        # (B3c: local agent scratch lives under the hidden .ironclad/agent/, not the project root)
        fb = tmp_path / ".ironclad" / "agent" / "feedback" / "KGC-7_OPUS-feedback.md"
        fb.parent.mkdir(parents=True, exist_ok=True)
        fb.write_text("## Result\ndone", encoding="utf-8")
        return _R()

    monkeypatch.setattr(client.subprocess, "run", _fake_run)
    item = {"id": "KGC-7", "agent": "OPUS",
            "handover_file": "KGC-7_OPUS.md", "handover": "do the thing"}
    out, _meta = client._run_handover(item, tmp_path, log=lambda *_: None)
    assert out and "done" in out
    argv = captured["argv"]
    assert "--permission-mode" in argv
    # the mode value follows the flag and is non-interactive (not "default")
    mode = argv[argv.index("--permission-mode") + 1]
    assert mode and mode != "default"
    assert "--print" in argv and "--model" in argv


def test_run_handover_launches_in_shipped_project_cwd(tmp_path, monkeypatch):
    # #1307: the coder builds PRODUCT CODE in the active project's code root shipped by /pending (`cwd`),
    # NOT the client's static startup codedir — else a coder launched after an in-session /switch writes
    # one project's code into another project's tree.
    captured = {}

    class _R:
        returncode = 0

    proj = tmp_path / "proj_code_root"
    proj.mkdir()

    def _fake_run(argv, **kw):
        captured["argv"] = argv
        captured["cwd"] = kw.get("cwd")
        return _R()

    monkeypatch.setattr(client.subprocess, "run", _fake_run)
    item = {"id": "KGC-7", "agent": "OPUS", "handover_file": "KGC-7_OPUS.md",
            "handover": "do the thing", "cwd": str(proj)}
    client._run_handover(item, tmp_path, log=lambda *_: None)
    assert captured["cwd"] == str(proj)                     # launched in the shipped project code root…
    assert captured["cwd"] != str(tmp_path)                 # …NOT the client codedir
    # the scratch stays under the client codedir and the coder is handed ABSOLUTE paths, so the feedback
    # round-trip is independent of the coder's cwd (the product tree).
    scratch_ho = tmp_path / ".ironclad" / "agent" / "handovers" / "KGC-7_OPUS.md"
    assert scratch_ho.exists()
    prompt_tok = [a for a in captured["argv"] if "KGC-7_OPUS.md" in a]
    assert prompt_tok and str(scratch_ho) in prompt_tok[0]  # the prompt names the ABSOLUTE scratch path


def test_run_handover_cwd_falls_back_to_codedir(tmp_path, monkeypatch):
    # #1307 back-compat: an older engine ships no `cwd` → the client launches in its own codedir (byte-identical).
    captured = {}

    class _R:
        returncode = 0

    def _fake_run(argv, **kw):
        captured["cwd"] = kw.get("cwd")
        return _R()

    monkeypatch.setattr(client.subprocess, "run", _fake_run)
    item = {"id": "KGC-7", "agent": "OPUS", "handover_file": "KGC-7_OPUS.md", "handover": "x"}
    client._run_handover(item, tmp_path, log=lambda *_: None)
    assert captured["cwd"] == str(tmp_path)


def test_run_handover_unusable_shipped_cwd_falls_back_to_codedir(tmp_path, monkeypatch):
    # #1307 (Codex review): a remote/sealed client does not share the server's filesystem, so an absolute
    # shipped cwd may not exist on THIS host — the client must fall back to its own codedir, not fail the
    # launch by spawning in a nonexistent directory.
    captured = {}

    class _R:
        returncode = 0

    def _fake_run(argv, **kw):
        captured["cwd"] = kw.get("cwd")
        return _R()

    monkeypatch.setattr(client.subprocess, "run", _fake_run)
    ghost = str(tmp_path / "does_not_exist_on_this_host")
    item = {"id": "KGC-7", "agent": "OPUS", "handover_file": "KGC-7_OPUS.md", "handover": "x", "cwd": ghost}
    client._run_handover(item, tmp_path, log=lambda *_: None)
    assert captured["cwd"] == str(tmp_path)     # fell back to codedir; did NOT spawn in the missing path


def test_build_argv_feedback_token_substitutes():
    # #443: the {feedback} token renders the result-capture path (e.g. Codex `-o {feedback}`); a template
    # that omits it (the Claude default) ignores the new optional arg.
    argv = client.build_agent_argv(
        "{bin} exec -o {feedback} {prompt}", bin="codex", model="m", effort="e",
        permission="p", prompt="do task", feedback=".ironclad/agent/feedback/T_CODEX-output.md")
    assert argv == ["codex", "exec", "-o", ".ironclad/agent/feedback/T_CODEX-output.md", "do task"]
    assert client.build_agent_argv("{bin} {prompt}", bin="x", model="m", effort="e",
                                    permission="p", prompt="p") == ["x", "p"]


def test_build_argv_mcp_is_a_multi_token_placeholder():
    # #480/#994-S10: {mcp} expands (via shlex) to 0+ args — the read-only Memory MCP config when
    # memory is configured and the agent ships an mcp_template, or NOTHING otherwise.
    t = "{bin} exec {mcp} --print {prompt}"
    assert client.build_agent_argv(t, bin="codex", model="m", effort="e", permission="p", prompt="go",
                                   mcp='-c a=1 -c b=2') == ["codex", "exec", "-c", "a=1", "-c", "b=2", "--print", "go"]
    assert client.build_agent_argv(t, bin="codex", model="m", effort="e", permission="p", prompt="go",
                                   mcp="") == ["codex", "exec", "--print", "go"]   # empty mcp ⇒ dropped


def _ho_item():
    return {"id": "T9", "agent": "CODEX", "handover_file": "T9_CODEX.md", "handover": "x"}


def test_run_handover_falls_back_to_captured_message(tmp_path, monkeypatch):
    # #443 (FORK-A2=C): the agent skipped the in-prompt feedback file but its `-o {feedback}` capture
    # exists → the runner returns the captured final message, not a silent no-feedback None.
    class _R:
        returncode = 0

    def _fake_run(argv, **kw):  # writes ONLY the -o capture, not the feedback file
        cap = tmp_path / ".ironclad" / "agent" / "feedback" / "T9_CODEX-output.md"
        cap.parent.mkdir(parents=True, exist_ok=True)
        cap.write_text("captured final message", encoding="utf-8")
        return _R()

    monkeypatch.setattr(client.subprocess, "run", _fake_run)
    assert client._run_handover(_ho_item(), tmp_path, log=lambda *_: None)[0] == "captured final message"


def test_run_handover_feedback_file_wins_over_capture(tmp_path, monkeypatch):
    # the agent-written feedback file is PRIMARY; the capture is only a fallback.
    class _R:
        returncode = 0

    def _fake_run(argv, **kw):
        d = tmp_path / ".ironclad" / "agent" / "feedback"
        d.mkdir(parents=True, exist_ok=True)
        (d / "T9_CODEX-feedback.md").write_text("the real feedback", encoding="utf-8")
        (d / "T9_CODEX-output.md").write_text("captured message", encoding="utf-8")
        return _R()

    monkeypatch.setattr(client.subprocess, "run", _fake_run)
    assert client._run_handover(_ho_item(), tmp_path, log=lambda *_: None)[0] == "the real feedback"


def test_run_handover_no_feedback_no_capture_is_none(tmp_path, monkeypatch):
    # NEGATIVE: neither the feedback file nor a non-empty capture → None (the existing no-feedback path).
    class _R:
        returncode = 1

    monkeypatch.setattr(client.subprocess, "run", lambda argv, **kw: _R())
    assert client._run_handover(_ho_item(), tmp_path, log=lambda *_: None)[0] is None


def test_run_handover_ignores_stale_capture_from_prior_run(tmp_path, monkeypatch):
    # #443 review F-1: a leftover -output.md from a PRIOR failed attempt must NOT be read as this run's
    # result — the runner unlinks both result files before launching. This run writes nothing → None.
    d = tmp_path / ".ironclad" / "agent" / "feedback"
    d.mkdir(parents=True, exist_ok=True)
    (d / "T9_CODEX-output.md").write_text("STALE message from a previous run", encoding="utf-8")

    class _R:
        returncode = 1

    monkeypatch.setattr(client.subprocess, "run", lambda argv, **kw: _R())  # writes nothing this run
    assert client._run_handover(_ho_item(), tmp_path, log=lambda *_: None)[0] is None


def test_run_handover_whitespace_capture_is_none(tmp_path, monkeypatch):
    # a capture that is whitespace-only is not a usable result → None (the text.strip() guard).
    class _R:
        returncode = 0

    def _fake_run(argv, **kw):
        cap = tmp_path / ".ironclad" / "agent" / "feedback" / "T9_CODEX-output.md"
        cap.parent.mkdir(parents=True, exist_ok=True)
        cap.write_text("   \n  ", encoding="utf-8")
        return _R()

    monkeypatch.setattr(client.subprocess, "run", _fake_run)
    assert client._run_handover(_ho_item(), tmp_path, log=lambda *_: None)[0] is None


def test_dispatch_claims_and_runs_each_once(monkeypatch, tmp_path):
    srv = _FakeServer(_items("KGC-1", "KGC-2"))
    ran = []
    monkeypatch.setattr(client, "_run_handover",
                        lambda item, codedir, log=print: (ran.append(item["id"]), (f"fb-{item['id']}", {}))[1])
    claimed: set = set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = client.dispatch_pending(srv, tmp_path, pool, claimed)
        wait(futs)
    assert sorted(ran) == ["KGC-1", "KGC-2"]
    assert claimed == {"KGC-1", "KGC-2"}
    assert sorted(u[0] for u in srv.uploaded) == ["KGC-1", "KGC-2"]


def test_already_claimed_not_resubmitted(monkeypatch, tmp_path):
    srv = _FakeServer(_items("KGC-1"))
    calls = []
    monkeypatch.setattr(client, "_run_handover",
                        lambda item, codedir, log=print: (calls.append(item["id"]), ("fb", {}))[1])
    claimed = {"KGC-1"}  # already in progress
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = client.dispatch_pending(srv, tmp_path, pool, claimed)
        wait(futs)
    assert futs == [] and calls == []  # nothing newly started


def test_runs_concurrently(monkeypatch, tmp_path):
    """3 handovers in a size-3 pool must run at the same time, not serially."""
    barrier = threading.Barrier(3, timeout=5)
    started = []

    def _blocking(item, codedir, log=print):
        started.append(item["id"])
        barrier.wait()  # all three must meet here -> real parallelism
        return f"fb-{item['id']}", {}                      # #455: (feedback, run-meta) tuple

    monkeypatch.setattr(client, "_run_handover", _blocking)
    srv = _FakeServer(_items("KGC-1", "KGC-2", "KGC-3"))
    claimed: set = set()
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = client.dispatch_pending(srv, tmp_path, pool, claimed)
        done, _ = wait(futs, timeout=8)
    assert len(done) == 3
    assert len(srv.uploaded) == 3  # barrier only reachable if all ran concurrently


def test_failure_unclaims_for_retry(monkeypatch, tmp_path):
    srv = _FakeServer(_items("KGC-1"))
    monkeypatch.setattr(client, "_run_handover", lambda item, codedir, log=print: (None, {}))  # no feedback
    claimed: set = set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        wait(client.dispatch_pending(srv, tmp_path, pool, claimed))
    assert claimed == set()       # released -> next poll retries
    # #455: the client now POSTS the run signal even with no feedback (so the server can classify a
    # budget-exhausted run + fail over) — but with EMPTY content, so no feedback file is written.
    assert srv.uploaded == [("KGC-1", "OPUS", "")]


def test_exception_unclaims(monkeypatch, tmp_path):
    srv = _FakeServer(_items("KGC-1"))

    def _boom(item, codedir, log=print):
        raise RuntimeError("claude crashed")

    monkeypatch.setattr(client, "_run_handover", _boom)
    claimed: set = set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        results = wait(client.dispatch_pending(srv, tmp_path, pool, claimed))
    assert claimed == set()
    # the job must NOT kill the whole loop — _process_one catches it
    assert all(f.result() is False for f in results.done)
