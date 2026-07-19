"""#1226 (S4) — the model-invokable trigger verb ``launch_coder``.

The orchestrator (the single steering author) launches the coder for a staged handover ON DEMAND — bypassing
the autopilot daemon (which stays off by default; ADR-0002 D7 / #312 S4: no second steering authority). These
tests drive the verb THROUGH the tool dispatcher and assert: it launches + flips the task to in_progress
WITHOUT ``AUTOPILOT_ENABLED``; it is a clear no-op when nothing is staged; it respects the concurrency cap and
the double-launch guard; it honours an explicit ``task_id``; and it fails closed on an unknown/absent agent.
Modelled on ``test_code_agent_registry.py`` (the ``_do_launch`` Popen-fake pattern).
"""
from __future__ import annotations

import os
import shlex
import sys
import threading
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
import providers  # noqa: E402

_TASK = {"type": "feature", "priority": "high", "title": "wire", "description": "x"}


class _FakeProc:
    """A just-spawned coder: still running (``poll``→None) and ``wait`` PARKS. A real coder runs for
    seconds; an instant exit is a test artefact that let the detached ``_wait`` monitor thread race
    ``_trigger_coder``'s synchronous status read and ``mark_blocked(errored)`` before it — flipping the
    ``OK:`` return to ``ERROR: coder exit 0, no feedback`` on unlucky scheduling (the pre-existing ±1
    count flake, #1432). Parking ``wait`` keeps the monitor from ever surfacing during the launch, so the
    return is deterministic; the daemon thread dies at interpreter exit."""
    pid = 1

    def __init__(self):
        self._parked = threading.Event()   # never set → the detached monitor blocks (daemon → dies at exit)

    def poll(self):
        return None                        # in-flight during the synchronous launch (not yet exited)

    def wait(self, *a, **k):
        self._parked.wait()                # block so `_surface_coder_result` never races the launch return
        return 0


def _setup(monkeypatch, tmp_path, *, agent="OPUS", stage=True, frontmatter="---\n---\nho", cfg=None):
    """Fresh engine state + an active initiative + (optionally) a pending task with a staged handover.
    Returns (tid, popen_calls); popen_calls records every ``Popen(argv)`` so a test can assert launch/no-launch."""
    cfg = cfg or gx10._code_defaults()
    gx10._apply_config(cfg)
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg)
    gx10.STORE = None
    gx10._AUTOPILOT_ACTIVE = 0            # isolate from any prior test's slot accounting
    gx10._AUTOPILOT_PROCS.clear()
    gx10._CODE_AGENT_BREAKER.clear()
    gx10._MODEL_CHECK_CACHE.clear()
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)  # cp1252-safe (the _wait daemon prints)
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Auto", "software")
    tid = gx10._store().create(dict(_TASK), force=True)["id"]
    if stage:
        (gx10.handovers_dir() / f"{tid}_{agent}.md").write_text(frontmatter, encoding="utf-8")
    popen_calls = []

    def _fake_popen(a, *args, **kw):
        popen_calls.append(list(a))
        return _FakeProc()

    monkeypatch.setattr(gx10.subprocess, "Popen", _fake_popen)
    return tid, popen_calls


def _launch(task_id=None):
    return gx10._run_tool_dispatch("launch_coder", {"task_id": task_id} if task_id else {})


def test_launch_coder_launches_staged_and_flips_in_progress(monkeypatch, tmp_path):
    tid, popen = _setup(monkeypatch, tmp_path)
    assert gx10.AUTOPILOT_ENABLED is False              # the verb must NOT require autopilot on
    out = _launch()
    assert out.startswith("OK:")
    assert len(popen) == 1                              # the coder was actually spawned
    assert gx10._store().get(tid)["status"] == "in_progress"


def test_launch_coder_refuses_unauthorized_envelope_without_popen(monkeypatch, tmp_path):
    from ack.tooling_envelope import load_tooling_envelope_policy
    tid, popen = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "TOOLING_ENVELOPE_POLICY", load_tooling_envelope_policy({
        "security": {"tooling_envelope": {
            "enabled": True,
            "allow_list": [{"bin": "other", "cmd_template": "{bin} --print {prompt}"}],
        }}
    }))
    out = _launch()
    assert out.startswith("OK:") or "launch" in out
    assert popen == []
    assert gx10._store().get(tid)["status"] == "pending"


def test_launch_coder_default_non_stream_uses_safe_permission_mode(monkeypatch, tmp_path):
    from ack.tooling_envelope import autopilot_claude_print_template, load_tooling_envelope_policy
    tid, popen = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "AUTOPILOT_STREAM", False, raising=False)
    monkeypatch.setattr(gx10, "TOOLING_ENVELOPE_POLICY", load_tooling_envelope_policy({
        "security": {"tooling_envelope": {
            "enabled": True,
            "allow_list": [{"bin": "claude", "cmd_template": autopilot_claude_print_template(stream=False)}],
        }}
    }))
    out = _launch()
    assert out.startswith("OK:")
    assert len(popen) == 1
    assert popen[0][:8] == [
        "claude", "--model", "claude-opus-4-8", "--effort", "high",
        "--permission-mode", "default", "--print",
    ]
    assert "--dangerously-skip-permissions" not in popen[0]
    assert gx10._store().get(tid)["status"] == "in_progress"


@pytest.mark.parametrize("memory_configured", [False, True], ids=["memory-off", "memory-on"])
def test_launch_coder_claude_memory_mcp_shape_and_env(monkeypatch, tmp_path, memory_configured):
    from ack.tooling_envelope import assert_authorized

    cfg = gx10._code_defaults()
    if memory_configured:
        cfg["code_agents"]["pool"][0]["mcp_template"] = (
            "--mcp-config '{\"mcpServers\":{\"memory\":{\"command\":\"{mcp_cmd}\","
            "\"args\":[\"{mcp_script}\"]}}}'"
        )
    tid, popen = _setup(monkeypatch, tmp_path, cfg=cfg)
    monkeypatch.setattr(gx10, "AUTOPILOT_STREAM", False, raising=False)
    monkeypatch.setattr(
        gx10,
        "_MEMORY_CONFIG",
        {"base_url": "http://memory:8800", "agent_id": "ironclad"} if memory_configured else {},
        raising=False,
    )
    resolved = []
    real_mcp_for_launch = gx10._mcp_for_launch

    def _tracked_mcp_for_launch(spec):
        result = real_mcp_for_launch(spec)
        resolved.append(result)
        return result

    launch_env = {}

    def _fake_popen(argv, *args, **kwargs):
        popen.append(list(argv))
        launch_env.update(kwargs["env"])
        return _FakeProc()

    monkeypatch.setattr(gx10, "_mcp_for_launch", _tracked_mcp_for_launch)
    monkeypatch.setattr(gx10.subprocess, "Popen", _fake_popen)

    assert _launch(task_id=tid).startswith("OK:")
    assert len(resolved) == 1
    mcp_args, mcp_env = resolved[0]
    mcp_tokens = shlex.split(mcp_args)
    assert popen[0] == [
        "claude", "--model", "claude-opus-4-8", "--effort", "high",
        "--permission-mode", "default", *mcp_tokens, "--print", popen[0][-1],
    ]
    assert bool(mcp_tokens) is memory_configured
    assert assert_authorized("claude", popen[0], gx10.TOOLING_ENVELOPE_POLICY)
    assert launch_env == {**os.environ, "PYTHONIOENCODING": "utf-8", **mcp_env}
    if not memory_configured:
        assert (mcp_args, mcp_env) == ("", {})


def test_launch_coder_config_agent_renders_memory_mcp_and_merges_env(monkeypatch, tmp_path):
    import commands

    cfg = gx10._code_defaults()
    cfg["code_agents"]["pool"][0].update({
        "provider_id": "codex",
        "agent_id": "CODEX",
        "display": "Codex",
        "model": "deployment-model",
        "bin": "codex",
        "cmd_template": "{bin} exec --model {model} {mcp} -o {feedback} {prompt}",
        "mcp_template": "-c 'mcp_servers.memory.command=\"{mcp_cmd}\"'",
    })
    tid, popen = _setup(monkeypatch, tmp_path, agent="CODEX", cfg=cfg)
    monkeypatch.setattr(
        gx10,
        "_MEMORY_CONFIG",
        {"base_url": "http://memory:8800", "agent_id": "ironclad"},
        raising=False,
    )
    resolved = []
    real_mcp_for_launch = gx10._mcp_for_launch

    def _tracked_mcp_for_launch(spec):
        result = real_mcp_for_launch(spec)
        resolved.append(result)
        return result

    rendered = []
    real_build_agent_argv = commands.build_agent_argv

    def _tracked_build_agent_argv(template, **kwargs):
        rendered.append((template, dict(kwargs)))
        return real_build_agent_argv(template, **kwargs)

    launch_env = {}

    def _fake_popen(argv, *args, **kwargs):
        popen.append(list(argv))
        launch_env.update(kwargs["env"])
        return _FakeProc()

    monkeypatch.setattr(gx10, "_mcp_for_launch", _tracked_mcp_for_launch)
    monkeypatch.setattr(commands, "build_agent_argv", _tracked_build_agent_argv)
    monkeypatch.setattr(gx10.subprocess, "Popen", _fake_popen)

    assert _launch(task_id=tid).startswith("OK:")
    assert len(resolved) == 1
    mcp_args, mcp_env = resolved[0]
    assert len(rendered) == 1 and rendered[0][1]["mcp"] == mcp_args
    assert shlex.split(mcp_args) == popen[0][4:6]
    assert launch_env == {**os.environ, "PYTHONIOENCODING": "utf-8", **mcp_env}
    assert launch_env["GX10_MEMORY_URL"] == "http://memory:8800"
    assert launch_env["GX10_MCP_MEMORY_NS"] == "ironclad"


def test_launch_coder_explicit_agent_capability_restores_permission_bypass(monkeypatch, tmp_path):
    cfg = gx10._code_defaults()
    cfg["code_agents"]["pool"][0]["permission_mode"] = "bypassPermissions"
    cfg["code_agents"]["pool"][0]["capabilities"] = {"permission_bypass": True}
    tid, popen = _setup(monkeypatch, tmp_path, cfg=cfg)

    out = _launch()

    assert out.startswith("OK:")
    assert len(popen) == 1
    assert "--dangerously-skip-permissions" in popen[0]
    assert "--permission-mode" not in popen[0]
    assert gx10._store().get(tid)["status"] == "in_progress"


def test_launch_coder_defers_to_auto_when_autopilot_owns_the_drive(monkeypatch, tmp_path):
    # #1309: with /auto on (autopilot + watcher, the meta-switch state) the loop launches staged handovers
    # itself — launch_coder must DEFER with a clear no-op instead of racing the loop for the single coder
    # slot (the "BUSY" collision + the contradictory double message in the design-driven loop).
    tid, popen = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "AUTOPILOT_ENABLED", True, raising=False)
    monkeypatch.setattr(gx10, "_WATCHER_ENABLED", True, raising=False)   # /auto meta-switch → loop is live
    out = _launch()
    assert "/auto owns launching" in out and tid in out
    assert len(popen) == 0                               # NOT launched here — the loop owns launching
    assert gx10._store().get(tid)["status"] == "pending" # untouched (no premature in_progress flip)


def test_launch_coder_still_launches_in_autopilot_only_mixed_state(monkeypatch, tmp_path):
    # #1309 (Codex review): the low-level `autopilot on` ALONE (watcher off + automation.decoupled False)
    # does NOT run the reconciler loop — nothing else launches — so launch_coder must NOT defer there, else
    # the staged task strands forever. It still launches manually (the guided fallback).
    tid, popen = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "AUTOPILOT_ENABLED", True, raising=False)
    monkeypatch.setattr(gx10, "_WATCHER_ENABLED", False, raising=False)
    monkeypatch.setattr(gx10, "AUTOMATION_DECOUPLED", False, raising=False)
    out = _launch()
    assert out.startswith("OK:") and "/auto owns launching" not in out
    assert len(popen) == 1                               # launched manually — not stranded
    assert gx10._store().get(tid)["status"] == "in_progress"


def test_launch_coder_surfaces_unknown_agent_even_under_auto(monkeypatch, tmp_path):
    # #1309 (Codex review): the /auto defer runs AFTER the fail-closed agent validation — an unknown/
    # disabled staged agent must still surface as an ERROR, never be masked by a misleading "auto owns" OK
    # (the reconciler skips unconfigured agents too, so the task would otherwise strand pending forever).
    tid, popen = _setup(monkeypatch, tmp_path, agent="NOPE")     # staged for an unconfigured agent
    monkeypatch.setattr(gx10, "AUTOPILOT_ENABLED", True, raising=False)
    monkeypatch.setattr(gx10, "_WATCHER_ENABLED", True, raising=False)
    out = _launch()
    assert "ERROR" in out and "/auto owns launching" not in out
    assert len(popen) == 0                                      # nothing launched; the config error surfaced


def test_launch_coder_noop_when_nothing_staged(monkeypatch, tmp_path):
    _tid, popen = _setup(monkeypatch, tmp_path, stage=False)   # a task exists but has NO handover
    out = _launch()
    assert "No staged handover" in out
    assert popen == []


def test_launch_coder_respects_max_concurrent(monkeypatch, tmp_path):
    tid, popen = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gx10, "AUTOPILOT_MAX_CONCURRENT", 1, raising=False)
    monkeypatch.setattr(gx10, "_autopilot_active", lambda: 1)  # one coder already running (deterministic)
    out = _launch()
    assert out.startswith("BUSY:")
    assert popen == []
    assert gx10._store().get(tid)["status"] == "pending"


def test_launch_coder_double_launch_guard(monkeypatch, tmp_path):
    tid, popen = _setup(monkeypatch, tmp_path)
    gx10._store().transition(tid, "in_progress")        # already running
    # the DEFAULT resolver scans only `pending`, so it naturally skips an in_progress task (no double-launch);
    # the explicit-id path still hits the guard with a clear message.
    out = _launch(task_id=tid)
    assert "already in_progress" in out
    assert popen == []


def test_launch_coder_default_skips_in_progress(monkeypatch, tmp_path):
    tid, popen = _setup(monkeypatch, tmp_path)
    gx10._store().transition(tid, "in_progress")        # the only unit is already running
    out = _launch()                                     # default resolution finds nothing new to launch
    assert "No staged handover" in out
    assert popen == []


def test_launch_coder_explicit_task_id(monkeypatch, tmp_path):
    tid, popen = _setup(monkeypatch, tmp_path)
    out = _launch(task_id=tid)
    assert out.startswith("OK:")
    assert len(popen) == 1
    assert gx10._store().get(tid)["status"] == "in_progress"


def test_launch_coder_unknown_task_id(monkeypatch, tmp_path):
    _tid, popen = _setup(monkeypatch, tmp_path)
    out = _launch(task_id="KGC-999")
    assert out.startswith("ERROR: no such task")
    assert popen == []


def test_launch_coder_unknown_agent_fails_closed(monkeypatch, tmp_path):
    tid, popen = _setup(monkeypatch, tmp_path, agent="ZZZ")   # ZZZ is not in the default OPUS/SONNET registry
    out = _launch()
    assert out.startswith("ERROR: unknown/unconfigured agent")
    assert popen == []
    assert gx10._store().get(tid)["status"] == "pending"


def test_launch_coder_server_topology_message_when_no_agents(monkeypatch, tmp_path):
    tid, popen = _setup(monkeypatch, tmp_path)
    # server topology = providers disabled → the registry is empty; the coder runs on the CLIENT, not here.
    class _EmptyReg:
        def has(self, name):
            return False

        def names(self):
            return []

    monkeypatch.setattr(gx10, "_code_agent_registry", lambda: _EmptyReg())
    monkeypatch.setattr(gx10, "_agent_names", lambda: [])
    out = _launch()
    assert "server topology" in out
    assert popen == []
    assert gx10._store().get(tid)["status"] == "pending"


def test_launch_coder_releases_slot_when_do_launch_raises(monkeypatch, tmp_path):
    # Finding #1: _do_launch can raise BEFORE its own release paths (a bad logdir, a non-KeyError transition
    # error). _trigger_coder must catch it and free the reserved slot — else every later launch reports BUSY
    # forever (the counter is permanently short one slot).
    _tid, popen = _setup(monkeypatch, tmp_path)

    def _boom(*a, **k):
        raise OSError("logdir unwritable")

    monkeypatch.setattr(gx10, "_do_launch", _boom)
    before = gx10._autopilot_active()
    out = _launch()
    assert out.startswith("ERROR")
    assert "slot released" in out
    assert gx10._autopilot_active() == before          # no slot leak


def test_launch_coder_error_when_spawn_fails(monkeypatch, tmp_path):
    # A Popen failure inside _do_launch's guarded block: it releases the slot + leaves the task pending; the
    # verb must report ERROR (not a false OK) and not flip the status.
    tid, _popen = _setup(monkeypatch, tmp_path)

    def _fail_popen(*a, **k):
        raise OSError("no agent binary")

    monkeypatch.setattr(gx10.subprocess, "Popen", _fail_popen)
    before = gx10._autopilot_active()
    out = _launch()
    assert out.startswith("ERROR")
    assert gx10._store().get(tid)["status"] == "pending"   # not flipped on a failed spawn
    assert gx10._autopilot_active() == before              # slot released


def test_launch_coder_cached_model_mismatch_marks_blocked_without_spawn(monkeypatch, tmp_path):
    tid, popen = _setup(monkeypatch, tmp_path)
    board_writes = []
    write_board = gx10._write_board

    def _record_board_write():
        board_writes.append(None)
        write_board()

    monkeypatch.setattr(gx10, "_write_board", _record_board_write)
    gx10._MODEL_CHECK_CACHE["OPUS"] = providers.ModelCheck(
        agent_id="OPUS",
        configured="claude-opus-4-8",
        ok=False,
        available=["claude-opus-4.1"],
        available_raw="claude-opus-4.1",
    )
    out = _launch(task_id=tid)
    t = gx10._store().get(tid)
    assert out.startswith("ERROR")
    assert popen == []
    assert t["status"] == "in_progress"
    assert t["blocked_kind"] == "errored"
    assert "not offered" in t["blocked_reason"]
    assert len(board_writes) == 1
    board = (gx10.vault_root() / gx10.active_slug() / gx10.BOARD_FILENAME).read_text(encoding="utf-8")
    assert f"`{tid}`" in board
    assert "⚠ ERRORED: agent OPUS: model" in board


def test_launch_coder_empty_model_cache_keeps_launch_path(monkeypatch, tmp_path):
    tid, popen = _setup(monkeypatch, tmp_path)
    out = _launch(task_id=tid)
    assert out.startswith("OK:")
    assert len(popen) == 1


def test_surface_coder_result_marks_failed_empty_run_blocked(monkeypatch, tmp_path):
    tid, _popen = _setup(monkeypatch, tmp_path)
    gx10._store().transition(tid, "in_progress")
    log = tmp_path / "coder.log"
    log.write_text("unknown model grok-build\n", encoding="utf-8")
    gx10._surface_coder_result(tid, "OPUS", 1, log)
    t = gx10._store().get(tid)
    assert t["blocked_kind"] == "errored"
    assert "coder exit 1" in t["blocked_reason"]
    assert "unknown model" in t["blocked_reason"]


def test_surface_coder_result_does_not_classify_merged_log_as_unavailable(monkeypatch, tmp_path):
    tid, _popen = _setup(monkeypatch, tmp_path)
    gx10._store().transition(tid, "in_progress")
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", gx10._code_defaults(), raising=False)
    log = tmp_path / "coder.log"
    log.write_text("normal stdout: rate limit and quota notes from the task\n", encoding="utf-8")
    gx10._CODE_AGENT_BREAKER.clear()
    gx10._surface_coder_result(tid, "OPUS", 1, log)
    t = gx10._store().get(tid)
    assert t["blocked_kind"] == "errored"
    assert t["blocked_kind"] != "unavailable"
    assert "coder exit 1" in t["blocked_reason"]
    assert "rate limit and quota" in t["blocked_reason"]
    assert not gx10._breaker_tripped("OPUS")


def test_surface_coder_result_with_usable_feedback_is_not_blocked(monkeypatch, tmp_path):
    tid, _popen = _setup(monkeypatch, tmp_path)
    gx10._store().transition(tid, "in_progress")
    (gx10.feedback_dir() / f"{tid}_OPUS-feedback.md").write_text("status: done\nok", encoding="utf-8")
    gx10._surface_coder_result(tid, "OPUS", 0, tmp_path / "missing.log")
    assert not gx10._store().get(tid).get("blocked")


def test_surface_coder_result_exit_zero_stamps_missing_done_status(monkeypatch, tmp_path):
    tid, _popen = _setup(monkeypatch, tmp_path)
    gx10._store().transition(tid, "in_progress")
    fb = gx10.feedback_dir() / f"{tid}_OPUS-feedback.md"
    fb.write_text("## Result\nall checks passed", encoding="utf-8")

    gx10._surface_coder_result(tid, "OPUS", 0, tmp_path / "missing.log")

    assert fb.read_text(encoding="utf-8").startswith("status: done\n")


@pytest.mark.parametrize("result", [1, None], ids=["nonzero", "unknown"])
def test_surface_coder_result_non_success_never_stamps_done(monkeypatch, tmp_path, result):
    tid, _popen = _setup(monkeypatch, tmp_path)
    gx10._store().transition(tid, "in_progress")
    fb = gx10.feedback_dir() / f"{tid}_OPUS-feedback.md"
    fb.write_text("## Result\npartial output", encoding="utf-8")

    gx10._surface_coder_result(tid, "OPUS", result, tmp_path / "missing.log")

    assert gx10._feedback_status(fb.read_text(encoding="utf-8")) == ""


def test_validate_code_agent_models_populates_cache_and_returns_mismatch(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, stage=False)
    spec = providers.ProviderSpec(
        provider_id="grok",
        kind=providers.ProviderKind.CLI,
        agent_id="GROK",
        model="grok-build",
        bin="grok",
        models_probe="models",
        cmd_template="{bin} -m {model} {prompt}",
    )

    class _Reg:
        def names(self):
            return ["GROK"]

        def resolve(self, aid):
            return spec

    class _CP:
        stdout = "grok-4.5\ngrok-composer-2.5-fast"
        stderr = ""

    monkeypatch.setattr(gx10, "_code_agent_registry", lambda: _Reg())
    monkeypatch.setattr(providers, "resolve_agent_bin", lambda s: "grok")
    monkeypatch.setattr(gx10.subprocess, "run", lambda *a, **k: _CP())
    mismatches = gx10._validate_code_agent_models()
    assert [m.agent_id for m in mismatches] == ["GROK"]
    assert gx10._MODEL_CHECK_CACHE["GROK"].configured == "grok-build"


def test_validate_code_agent_models_probe_failure_is_empty_fail_soft(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, stage=False)
    spec = providers.ProviderSpec(
        provider_id="grok",
        kind=providers.ProviderKind.CLI,
        agent_id="GROK",
        model="grok-build",
        bin="grok",
        models_probe="models",
        cmd_template="{bin} -m {model} {prompt}",
    )

    class _Reg:
        def names(self):
            return ["GROK"]

        def resolve(self, aid):
            return spec

    monkeypatch.setattr(gx10, "_code_agent_registry", lambda: _Reg())
    monkeypatch.setattr(providers, "resolve_agent_bin", lambda s: "grok")

    def _boom(*a, **k):
        raise TimeoutError("hung")

    monkeypatch.setattr(gx10.subprocess, "run", _boom)
    assert gx10._validate_code_agent_models() == []
