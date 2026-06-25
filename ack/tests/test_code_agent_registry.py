"""Config-driven code-agent registry (#449, epic #440 Phase 3, charter §C0R-9).

The handover code-AGENT identity lives in a SEPARATE, always-on config surface
(``config.code_agents.pool``) — a ``providers.CodeAgentRegistry`` keyed by ``agent_id`` — NOT in the
fan-out ``providers.pool``. These tests pin the spine that retired the six OPUS/SONNET allowlists,
``client._MODEL_BY_AGENT`` and the legacy KIMI→SONNET normalization:

  * the registry loads the public defaults (OPUS/SONNET) and a config pool that ADDS a third agent,
  * an UNKNOWN agent fails closed everywhere (registry, the two gx10 guards, the server pull/feedback),
  * every ``agent_id`` is an ASCII-letters-only filename token that round-trips through BOTH filename
    regexes (``_HO_AGENT_RE`` AND ``_FB_RE``) — the property that keeps the file contract intact,
  * the handover schema enum is generated from the LIVE registry,
  * the server resolves the FULL agent spec into the ``/pending`` item; the client only renders it.

The registry is agent-AGNOSTIC: these tests use a SYNTHETIC third agent (``TOOLX``) so they prove the
mechanism without baking a specific private backend into the public suite (the real agents live in
``conf/``). The one exception is the KIMI-retirement negative test, which names the retired legacy alias.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10        # noqa: E402
import server      # noqa: E402
import providers   # noqa: E402
from providers import (CodeAgentRegistry, ProviderSpec, load_code_agents,  # noqa: E402
                       probe_code_agents, resolve_agent_bin)
from pydantic import ValidationError  # noqa: E402

_TASK = {"type": "feature", "priority": "high", "title": "wire", "description": "x"}


def _cfg_with_extra_agent() -> dict:
    """Public defaults (OPUS/SONNET) + a GENERIC third agent (``TOOLX``) — the way ``conf/`` extends
    the pool (lists replace on merge). Synthetic on purpose: proves the registry is agent-agnostic."""
    return {"code_agents": {"pool": [
        {"provider_id": "claude-opus", "kind": "cli", "agent_id": "OPUS",
         "model": "claude-opus-4-8", "bin": "claude", "cmd_template": "{bin} --print {prompt}"},
        {"provider_id": "claude-sonnet", "kind": "cli", "agent_id": "SONNET",
         "model": "claude-sonnet-4-6", "bin": "claude", "cmd_template": "{bin} --print {prompt}"},
        {"provider_id": "toolx", "kind": "cli", "agent_id": "TOOLX", "model": "toolx-1",
         "bin": "toolx", "cmd_template": "{bin} run -o {feedback} {prompt}"},
    ]}}


# ── Registry loading + the public defaults ──────────────────────────────────────────────────────
def test_default_registry_has_opus_and_sonnet():
    reg = load_code_agents(gx10._code_defaults())
    assert reg.names() == ["OPUS", "SONNET"]            # declaration order, public defaults
    opus = reg.resolve("OPUS")
    assert opus.model == "claude-opus-4-8" and opus.bin == "claude"
    assert reg.resolve("sonnet").model == "claude-sonnet-4-6"   # case-insensitive lookup


def test_config_pool_adds_a_third_agent():
    reg = load_code_agents(_cfg_with_extra_agent())
    assert reg.names() == ["OPUS", "SONNET", "TOOLX"]
    toolx = reg.resolve("TOOLX")
    assert toolx.bin == "toolx" and "run" in toolx.cmd_template


# ── Unknown agent fails closed (the core invariant) ─────────────────────────────────────────────
@pytest.mark.parametrize("bogus", ["GROK", "TOOLX", "BOGUS", "", "opus2", "claude_opus"])
def test_unknown_agent_resolves_none_on_default_registry(bogus):
    reg = load_code_agents(gx10._code_defaults())
    assert reg.resolve(bogus) is None
    assert reg.has(bogus) is False


def test_kimi_is_not_silently_normalized_to_sonnet():
    # The legacy KIMI→SONNET alias is RETIRED: on a registry without a KIMI entry, KIMI is unknown,
    # not silently SONNET. (Negative test for the retired normalization.)
    reg = load_code_agents(gx10._code_defaults())
    assert reg.resolve("KIMI") is None
    assert "KIMI" not in reg.names()


# ── Validation: dup-guard, missing id, ASCII-letters-only, complete launch spec ──────────────────
def test_duplicate_agent_id_fails_loud():
    pool = {"code_agents": {"pool": [
        {"provider_id": "a", "kind": "cli", "agent_id": "OPUS", "model": "m", "bin": "x",
         "cmd_template": "{bin} {prompt}"},
        {"provider_id": "b", "kind": "cli", "agent_id": "OPUS", "model": "m2", "bin": "y",
         "cmd_template": "{bin} {prompt}"},
    ]}}
    with pytest.raises(ValueError, match="duplicate code-agent agent_id"):
        load_code_agents(pool)


def test_code_agent_without_agent_id_fails_loud():
    pool = {"code_agents": {"pool": [
        {"provider_id": "routing-only", "kind": "cli", "model": "m", "bin": "x",
         "cmd_template": "{bin} {prompt}"},
    ]}}
    with pytest.raises(ValueError, match="has no agent_id"):
        load_code_agents(pool)


@pytest.mark.parametrize("bad", ["CLAUDE_OPUS", "OPUS2", "OP-US", "codex cli", "OPUS.MD", "ÄGENT", "Ωmega"])
def test_agent_id_must_be_ascii_letters_only(bad):
    # §C0R-1 + review B-4: a non-ASCII-letters token would NOT round-trip the ASCII-only filename
    # regexes — reject it at construction (str.isalpha() alone would wrongly accept "ÄGENT").
    with pytest.raises(ValidationError):
        ProviderSpec(provider_id="p", kind="cli", model="m", agent_id=bad,
                     bin="x", cmd_template="{bin} {prompt}")


@pytest.mark.parametrize("missing", ["bin", "cmd_template"])
def test_code_agent_needs_both_bin_and_cmd_template(missing):
    # review B-5: a code-agent must ship a COMPLETE launch spec; a partial entry would silently mix a
    # configured field with the client's Claude fallback and emit a broken command. Fail loud.
    entry = {"provider_id": "x", "kind": "cli", "agent_id": "OPUS", "model": "m",
             "bin": "claude", "cmd_template": "{bin} {prompt}"}
    del entry[missing]
    with pytest.raises(ValueError, match="must define BOTH bin and cmd_template"):
        load_code_agents({"code_agents": {"pool": [entry]}})


def test_routing_only_provider_keeps_agent_id_none():
    spec = ProviderSpec(provider_id="spark", kind="in-engine", model="m", endpoint_env="E")
    assert spec.agent_id is None and spec.agent_display() == "spark"


# ── Filename-token round-trip vs BOTH regexes (C0R-1 / C0R-7) ───────────────────────────────────
@pytest.mark.parametrize("aid", ["OPUS", "SONNET", "TOOLX", "AGENTX"])
def test_agent_id_roundtrips_both_filename_regexes(aid):
    tid = "KGC-123"
    ho = gx10._HO_AGENT_RE.search(f"{tid}_{aid}.md")
    fb = gx10._FB_RE.search(f"{tid}_{aid}-feedback.md")
    assert ho and ho.group(1).upper() == aid           # handover filename token
    assert fb and fb.group(1).upper() == aid           # feedback filename token
    assert gx10._agent_from_handover(f"{tid}_{aid}.md") == aid


def test_non_letters_token_does_not_roundtrip_HO_RE():
    # WHY ASCII-letters-only is enforced (§C0R-1), demonstrated against the real regexes:
    #  (a) a DIGIT token fails _HO_AGENT_RE entirely → the agent resolves to "" → fail-closed.
    assert gx10._HO_AGENT_RE.search("KGC-1_OPUS2.md") is None
    assert gx10._agent_from_handover("KGC-1_OPUS2.md") == ""
    #  (b) an UNDERSCORE token mis-parses: _HO_AGENT_RE greedily matches only the LAST `_letters.md`
    #      segment, so `_CLAUDE_OPUS.md` resolves to "OPUS" (the WRONG agent), not "CLAUDE_OPUS".
    assert gx10._agent_from_handover("KGC-1_CLAUDE_OPUS.md") == "OPUS"
    assert gx10._agent_from_handover("KGC-1_CLAUDE_OPUS.md") != "CLAUDE_OPUS"


def test_both_regexes_parse_multisegment_filenames_consistently():
    # Review A: _FB_RE and _HO_AGENT_RE must extract the SAME token from a multi-segment filename,
    # else the handover side and the feedback side would identify DIFFERENT agents. Both letters-only.
    ho = gx10._HO_AGENT_RE.search("KGC-1_CLAUDE_OPUS.md")
    fb = gx10._FB_RE.search("KGC-1_CLAUDE_OPUS-feedback.md")
    assert ho.group(1) == fb.group(1) == "OPUS"        # symmetric trailing-segment parse
    assert gx10._HO_AGENT_RE.search("KGC-1_OPUS2.md") is None
    assert gx10._FB_RE.search("KGC-1_OPUS2-feedback.md") is None


# ── gx10 guards: config-driven membership, fail-closed (retired allowlists) ──────────────────────
def test_stage_handover_rejects_unknown_agent():
    out = gx10._stage_handover(None, "KIMI", "## handover\nbody")
    assert "unknown agent" in out and "KIMI" in out


def test_advance_pipeline_rejects_unknown_agent():
    out = gx10._advance_pipeline("KGC-1", "BOGUS")
    assert "unknown agent" in out and "BOGUS" in out


def test_known_agent_passes_the_membership_guard(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = gx10._advance_pipeline("KGC-1", "OPUS")
    assert "unknown agent" not in out


def test_task_agent_ignores_model_string_assigned_to(monkeypatch):
    # review B-3: a model string in assigned_to must NOT mis-resolve to an agent by loose substring.
    monkeypatch.setattr(gx10, "_find_handover", lambda *a, **k: None)
    assert gx10._task_agent({"assigned_to": "claude-opus-4-8"}) == ""   # no standalone "opus" token
    assert gx10._task_agent({"assigned_to": "OPUS"}) == "OPUS"          # a real agent token still matches
    assert gx10._task_agent({"assigned_to": "assigned to sonnet"}) == "SONNET"


# ── Dynamic schema enum from the live registry ──────────────────────────────────────────────────
def _agent_enums(tools):
    return [t["function"]["parameters"]["properties"]["agent"]["enum"]
            for t in tools
            if "agent" in t.get("function", {}).get("parameters", {}).get("properties", {})]


def test_schema_enum_default_is_opus_sonnet():
    enums = _agent_enums(gx10._tools_with_agent_enum(gx10.TOOLS))
    assert enums and all(e == ["OPUS", "SONNET"] for e in enums)
    static = _agent_enums(gx10.TOOLS)                  # the static TOOLS list is never mutated
    assert all(e == ["OPUS", "SONNET"] for e in static)


def test_schema_enum_tracks_config_added_agent(monkeypatch):
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", _cfg_with_extra_agent(), raising=False)
    enums = _agent_enums(gx10._tools_with_agent_enum(gx10.TOOLS))
    assert enums and all(e == ["OPUS", "SONNET", "TOOLX"] for e in enums)


# ── _do_launch (autopilot): byte-identical Claude shape + feedback path for templated agents ─────
def _capture_launch_argv(monkeypatch, tmp_path, agent, *, frontmatter, reg_cfg=None):
    gx10._apply_config(gx10._code_defaults())
    gx10.STORE = None
    # _do_launch spawns a monitor thread that _ui_prints a ✓ on completion; on a cp1252 test console
    # that daemon-thread print would raise UnicodeEncodeError. Silence it so the test is clean + stable.
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Auto", "software")
    if reg_cfg is not None:                            # override the registry AFTER _apply_config reset it
        monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", reg_cfg, raising=False)
    tid = gx10._store().create(dict(_TASK), force=True)["id"]
    (gx10.handovers_dir() / f"{tid}_{agent}.md").write_text(frontmatter, encoding="utf-8")
    captured = {}

    class _FakeProc:
        pid = 1
        def poll(self): return 0
        def wait(self, *a, **k): return 0

    def _fake_popen(a, *args, **kw):
        captured["argv"] = list(a)
        return _FakeProc()

    monkeypatch.setattr(gx10.subprocess, "Popen", _fake_popen)
    gx10._autopilot_reserve()
    gx10._do_launch(tid, agent)
    return captured.get("argv"), tid


def test_do_launch_default_claude_keeps_stream_plumbing(tmp_path, monkeypatch):
    # review B-1: the default OPUS/SONNET launch must stay byte-identical — the Claude `--print` shape
    # keeps --verbose + --output-format stream-json even though the defaults now carry a cmd_template.
    monkeypatch.setattr(gx10, "AUTOPILOT_STREAM", True, raising=False)
    argv, _ = _capture_launch_argv(monkeypatch, tmp_path, "OPUS",
                                   frontmatter="---\nto: claude-opus-4-8\n---\nho")
    assert "--print" in argv and "--verbose" in argv
    assert "--output-format" in argv and "stream-json" in argv


def test_do_launch_templated_agent_renders_feedback_path(tmp_path, monkeypatch):
    # review B-2: a templated non-Claude agent ({feedback} in the template) must get a NON-empty
    # capture path — a bare `-o` with an empty argument would write nowhere.
    reg_cfg = {**gx10._code_defaults(), "code_agents": _cfg_with_extra_agent()["code_agents"]}
    argv, tid = _capture_launch_argv(monkeypatch, tmp_path, "TOOLX",
                                     frontmatter="---\n---\nho", reg_cfg=reg_cfg)
    assert "--print" not in argv                       # NOT the Claude shape
    assert "-o" in argv
    cap = argv[argv.index("-o") + 1]
    assert cap and cap.endswith(f"{tid}_TOOLX-feedback.md")


# ── Server resolves the FULL spec into the /pending item; client renders it ─────────────────────
def _stage_handover_file(tmp_path, monkeypatch, token: str, frontmatter: str = "") -> str:
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Demo", "software")
    store = gx10._store()
    store.create(dict(_TASK), force=True)
    tid = store.list("pending")[0]["id"]
    ho_dir = gx10.handovers_dir()
    ho_dir.mkdir(parents=True, exist_ok=True)
    (ho_dir / f"{tid}_{token}.md").write_text(frontmatter + "body", encoding="utf-8")
    return tid


def test_pending_handover_embeds_full_spec(tmp_path, monkeypatch):
    _stage_handover_file(tmp_path, monkeypatch, "OPUS")
    pend = server._pending_handovers()
    assert len(pend) == 1
    item = pend[0]
    assert item["agent"] == "OPUS"
    assert item["model"] == "claude-opus-4-8"          # from the registry spec
    assert item["bin"] == "claude"
    assert item["cmd_template"] and "{prompt}" in item["cmd_template"]
    assert item["permission"] == "acceptEdits"


def test_pending_handover_frontmatter_overrides_spec_model(tmp_path, monkeypatch):
    _stage_handover_file(tmp_path, monkeypatch, "SONNET",
                         frontmatter="---\nto: claude-opus-4-8\neffort: low\n---\n")
    item = server._pending_handovers()[0]
    assert item["agent"] == "SONNET"
    assert item["model"] == "claude-opus-4-8"          # frontmatter `to:` wins over the spec default
    assert item["effort"] == "low"


def test_pending_skips_unknown_agent_handover(tmp_path, monkeypatch):
    _stage_handover_file(tmp_path, monkeypatch, "BOGUS")
    assert server._pending_handovers() == []           # not dispatchable → skipped (fail-closed)


def test_client_renders_server_supplied_spec():
    import client
    item = {"id": "KGC-9", "agent": "TOOLX", "model": "toolx-1", "bin": "toolx",
            "cmd_template": "{bin} run -o {feedback} {prompt}", "permission": None}
    argv = client.build_agent_argv(
        item["cmd_template"], bin=item["bin"], model=item["model"], effort="",
        permission=item.get("permission") or client.CLAUDE_PERMISSION_MODE,
        prompt="multi word task", feedback="cap.md")
    assert argv[0] == "toolx" and "run" in argv
    assert argv[-1] == "multi word task"               # prompt stays one arg
    assert "claude" not in argv                          # the client did NOT fall back to Claude


# ── Boot probe (#451): per-agent bin resolution, fail-closed only if ZERO resolve ───────────────
def _cli_spec(**kw):
    base = dict(provider_id="p", kind="cli", model="m", agent_id="OPUS",
                bin="x", cmd_template="{bin} {prompt}")
    base.update(kw)
    return ProviderSpec(**base)


def test_resolve_agent_bin_prefers_path_shim(monkeypatch):
    import providers
    monkeypatch.setattr(providers.shutil, "which", lambda b: "/usr/bin/" + b if b == "mytool" else None)
    spec = _cli_spec(bin="mytool", bin_glob="/should/not/be/used/*")
    assert resolve_agent_bin(spec) == "/usr/bin/mytool"     # PATH shim (option B) wins over the glob


def test_resolve_agent_bin_globs_when_not_on_path(tmp_path, monkeypatch):
    import providers
    monkeypatch.setattr(providers.shutil, "which", lambda b: None)   # not on PATH
    (tmp_path / "hash1").mkdir()
    exe = tmp_path / "hash1" / "codex.exe"
    exe.write_text("x")
    spec = _cli_spec(bin="codex", bin_glob=str(tmp_path / "*" / "codex.exe"))
    assert resolve_agent_bin(spec) == str(exe)             # option C: glob match when bin not on PATH


def test_resolve_agent_bin_glob_returns_newest(tmp_path, monkeypatch):
    import os
    import providers
    monkeypatch.setattr(providers.shutil, "which", lambda b: None)
    for i, name in enumerate(("old", "new")):
        d = tmp_path / name
        d.mkdir()
        (d / "codex.exe").write_text("x")
    old = tmp_path / "old" / "codex.exe"
    new = tmp_path / "new" / "codex.exe"
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))                  # newer mtime → the live launcher
    spec = _cli_spec(bin="codex", bin_glob=str(tmp_path / "*" / "codex.exe"))
    assert resolve_agent_bin(spec) == str(new)


def test_resolve_agent_bin_expands_env(tmp_path, monkeypatch):
    import providers
    monkeypatch.setattr(providers.shutil, "which", lambda b: None)
    monkeypatch.setenv("MY_CODEX_ROOT", str(tmp_path))
    (tmp_path / "h").mkdir()
    exe = tmp_path / "h" / "codex.exe"
    exe.write_text("x")
    spec = _cli_spec(bin="codex", bin_glob="$MY_CODEX_ROOT/*/codex.exe")
    assert resolve_agent_bin(spec) == str(exe)             # env var in bin_glob is expanded


def test_resolve_agent_bin_none_when_unresolvable(monkeypatch):
    import providers
    monkeypatch.setattr(providers.shutil, "which", lambda b: None)
    assert resolve_agent_bin(_cli_spec(bin="nope", bin_glob="/no/such/*/codex.exe")) is None
    assert resolve_agent_bin(_cli_spec(bin="nope", bin_glob=None)) is None
    assert resolve_agent_bin(None) is None


def test_resolve_agent_bin_skips_non_file_glob_matches(tmp_path, monkeypatch):
    # review A: a glob match that is a directory (not a regular file) must be skipped, not returned,
    # and stat'ing happens defensively (a vanished match never raises) → None when no real file.
    import providers
    monkeypatch.setattr(providers.shutil, "which", lambda b: None)
    (tmp_path / "hashdir").mkdir()                          # matches `*` but is NOT a file
    assert resolve_agent_bin(_cli_spec(bin="codex", bin_glob=str(tmp_path / "*"))) is None


def test_resolve_agent_bin_normalizes_forward_slash_glob(tmp_path, monkeypatch):
    # review A (S3): a conf glob may use `/` even where the OS separator is `\` — normalize so it resolves.
    import os
    import providers
    monkeypatch.setattr(providers.shutil, "which", lambda b: None)
    (tmp_path / "h").mkdir()
    exe = tmp_path / "h" / "codex.exe"
    exe.write_text("x")
    spec = _cli_spec(bin="codex", bin_glob=str(tmp_path).replace(os.sep, "/") + "/*/codex.exe")
    assert resolve_agent_bin(spec) == str(exe)


def test_probe_cli_available_if_any_agent_resolves(monkeypatch):
    import providers
    # OPUS resolves on PATH; SONNET does not (no bin_glob) → cli-available stays True (any resolves).
    monkeypatch.setattr(providers.shutil, "which", lambda b: "/usr/bin/claude" if b == "claude" else None)
    reg = load_code_agents(gx10._code_defaults())          # OPUS+SONNET, both bin="claude"
    probe = probe_code_agents(reg)
    assert set(probe) == {"OPUS", "SONNET"}
    assert all(p == "/usr/bin/claude" for p in probe.values())
    assert any(probe.values()) is True                     # cli-available


def test_probe_fail_closed_when_zero_resolve(monkeypatch):
    import providers
    monkeypatch.setattr(providers.shutil, "which", lambda b: None)   # nothing on PATH, no bin_glob
    reg = load_code_agents(gx10._code_defaults())
    probe = probe_code_agents(reg)
    assert probe == {"OPUS": None, "SONNET": None}
    assert any(probe.values()) is False                    # → boot treats as no local agent (fail-closed)


def test_client_falls_back_to_claude_when_spec_omitted():
    import client
    # An item without bin/cmd_template (e.g. an older server) → byte-identical Claude default.
    argv = client.build_agent_argv(client.AGENT_CMD, bin=client.CLAUDE_BIN, model="claude-opus-4-8",
                                   effort="high", permission=client.CLAUDE_PERMISSION_MODE, prompt="a b")
    assert argv[0] == client.CLAUDE_BIN and "--print" in argv and argv[-1] == "a b"


def test_run_handover_precedence_override_vs_server_spec(tmp_path, monkeypatch):
    # review B (round 4): an EXPLICIT client-side GX10_AGENT_CMD/GX10_CLAUDE_BIN must WIN over the
    # server-sent registry spec; without it the server spec is authoritative.
    import client
    item = {"id": "KGC-3", "agent": "OPUS", "model": "claude-opus-4-8", "bin": "claude",
            "cmd_template": "claude --model {model} --print {prompt}", "permission": "acceptEdits"}
    captured = {}

    class _P:
        returncode = 0

    def _fake_run(argv, **kw):
        captured["argv"] = argv
        return _P()

    monkeypatch.setattr(client.subprocess, "run", _fake_run)

    # (1) no explicit override → the server-sent spec is used
    monkeypatch.setattr(client, "AGENT_CMD_OVERRIDE", None, raising=False)
    monkeypatch.setattr(client, "CLAUDE_BIN_OVERRIDE", None, raising=False)
    client._run_handover(item, tmp_path, log=lambda *a, **k: None)
    assert captured["argv"][0] == "claude" and "--model" in captured["argv"]

    # (2) explicit GX10_AGENT_CMD + GX10_CLAUDE_BIN → the client override WINS over the server spec.
    # The template uses {bin}, so argv[0] proves the BIN override substituted (beating the server's
    # bin="claude"), not just a hardcoded literal.
    monkeypatch.setattr(client, "AGENT_CMD_OVERRIDE", "{bin} --go {prompt}", raising=False)
    monkeypatch.setattr(client, "CLAUDE_BIN_OVERRIDE", "mytool", raising=False)
    client._run_handover(item, tmp_path, log=lambda *a, **k: None)
    assert captured["argv"][0] == "mytool" and "--go" in captured["argv"]   # bin + template override won
    assert "--model" not in captured["argv"]           # the server's Claude template did NOT win


# ── #455: budget-exhausted result classifier (layered, conservative) ─────────────────────────────
_EXH = {"stderr_patterns": [r"(?i)\b(quota|rate limit)\b"], "exit_codes": [42],
        "json_event_types": ["budget_exhausted"]}


def test_classify_ok_when_feedback():
    assert providers.classify_agent_result(exit_code=0, stderr="", has_feedback=True,
                                           patterns=_EXH) == providers.RESULT_OK


def test_classify_feedback_wins_even_if_content_mentions_quota():
    # review B (S2): the FEEDBACK content is the agent's task result (a coding answer may legitimately
    # contain "rate limit"/"quota") — a real result is NEVER re-judged as exhausted, even if stderr
    # ALSO matched. Only the raw stderr is scanned, and only when there is NO feedback.
    assert providers.classify_agent_result(exit_code=0, stderr="implemented rate limit handling",
                                           has_feedback=True, patterns=_EXH) == providers.RESULT_OK


def test_classify_task_failed_no_feedback_no_signal():
    # an unknown failure is task-failed, NOT agent-unavailable — a normal failure must not failover.
    assert providers.classify_agent_result(exit_code=1, stderr="some build error",
                                           has_feedback=False, patterns=_EXH) == providers.RESULT_FAILED


@pytest.mark.parametrize("kw", [
    {"stderr": "Error: quota exceeded"},                 # stderr regex
    {"stderr": "you hit the rate limit"},                # stderr regex
    {"exit_code": 42},                                   # exit code
    {"stderr": '{"type": "budget_exhausted"}'},          # structured JSON event (one object per stderr line)
])
def test_classify_agent_unavailable_layers(kw):
    base = dict(exit_code=1, stderr="", has_feedback=False, patterns=_EXH)
    base.update(kw)
    assert providers.classify_agent_result(**base) == providers.RESULT_UNAVAILABLE


def test_classify_never_raises_on_bad_regex():
    bad = {"stderr_patterns": ["(unclosed"]}             # invalid regex from conf → skipped, no crash
    assert providers.classify_agent_result(exit_code=1, stderr="x", has_feedback=False,
                                           patterns=bad) == providers.RESULT_FAILED


def test_classify_no_patterns_never_unavailable():
    # without configured patterns there is no exhausted signal → never a false failover.
    assert providers.classify_agent_result(exit_code=1, stderr="quota exceeded",
                                           has_feedback=False, patterns=None) == providers.RESULT_FAILED


def test_default_exhausted_patterns_catch_common_signals():
    pats = gx10._code_defaults()["code_agents"]["exhausted"]
    for s in ("HTTP 429 Too Many Requests", "You have hit your usage limit", "insufficient credit"):
        assert providers.classify_agent_result(exit_code=1, stderr=s, has_feedback=False,
                                               patterns=pats) == providers.RESULT_UNAVAILABLE


# ── #460: onboarded-but-disabled agent (enabled:false until calibrated) — INERT but VISIBLE ──────
def _cfg_with_disabled_agent() -> dict:
    """OPUS/SONNET enabled + a GENERIC onboarded-but-DISABLED third agent (like KIMI pending its
    exhausted-signal calibration). Agent-agnostic on purpose."""
    cfg = _cfg_with_extra_agent()
    cfg["code_agents"]["pool"][2]["enabled"] = False     # TOOLX onboarded but not yet activated
    return cfg


def test_disabled_agent_is_inert_but_visible():
    reg = load_code_agents(_cfg_with_disabled_agent())
    # enabled-only launch/schema surface excludes it → never offered, never launchable, never resolvable
    assert reg.names() == ["OPUS", "SONNET"]
    assert reg.has("TOOLX") is False and reg.resolve("TOOLX") is None
    assert "TOOLX" not in reg.by_agent()
    # but it IS registered → visible for the operator (onboarding state)
    assert reg.all_ids() == ["OPUS", "SONNET", "TOOLX"]
    assert reg.spec_of("TOOLX") is not None and reg.spec_of("TOOLX").agent_id == "TOOLX"


def test_disabled_agent_must_still_be_well_formed():
    # validate_loud checks EVERY entry incl. disabled — an onboarded agent must ship a complete spec
    cfg = _cfg_with_disabled_agent()
    cfg["code_agents"]["pool"][2].pop("cmd_template")    # break the disabled entry
    with pytest.raises(ValueError, match="must define BOTH bin and cmd_template"):
        load_code_agents(cfg).validate_loud()


def test_disabled_agent_is_never_a_failover_peer(monkeypatch):
    # #455/#456: a tripped agent fails over to the cheapest AVAILABLE peer; a disabled agent (resolve→None)
    # must never be chosen, even if its id is listed in a class.
    cfg = _cfg_with_disabled_agent()
    cfg["code_agents"].setdefault("classes", {})["coding"] = ["OPUS", "SONNET", "TOOLX"]
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg, raising=False)
    gx10._CODE_AGENT_BREAKER.clear()
    gx10._breaker_trip("OPUS")
    gx10._breaker_trip("SONNET")
    # OPUS+SONNET tripped, TOOLX disabled → no available peer → keep the chosen agent (never the disabled one)
    assert gx10._effective_code_agent("OPUS", task_class="coding") == "OPUS"
    gx10._CODE_AGENT_BREAKER.clear()
