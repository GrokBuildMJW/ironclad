"""#1221: the generic cross-model second-opinion `review` tool.

Capability-detected (a runnable code-agent on the box), registry-resolved, runs via the existing
``client.default_cli_runner`` (mocked — no real CLI, no network). A READ → ``_INGESTION_TOOLS``.
Diff-mode (default git diff) + paths-mode (docs/decisions/artifacts); agent selection + anti-affinity.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
import providers  # noqa: E402
from providers import ProviderSpec, load_code_agents  # noqa: E402


def _spec(agent_id="SONNET", bin="claude", model="claude-sonnet-5"):
    return ProviderSpec(
        provider_id=f"cli-{agent_id.lower()}",
        kind="cli",
        agent_id=agent_id,
        model=model,
        bin=bin,
        cmd_template="{bin} --model {model} --print {prompt}",
        effort="high",
        permission_mode="bypassPermissions",
        capabilities={"permission_bypass": True},
    )


def _cfg_two_agents():
    return {"code_agents": {"pool": [
        {"provider_id": "claude-opus", "kind": "cli", "agent_id": "OPUS",
         "model": "claude-opus-4-8", "bin": "claude",
         "cmd_template": "{bin} --model {model} --print {prompt}", "effort": "xhigh"},
        {"provider_id": "claude-sonnet", "kind": "cli", "agent_id": "SONNET",
         "model": "claude-sonnet-5", "bin": "claude",
         "cmd_template": "{bin} --model {model} --print {prompt}", "effort": "high"},
    ]}}


def _make_runnable(monkeypatch, agents=("OPUS", "SONNET")):
    """Registry + bin-resolve so _review_available / _pick_reviewer see runnable agents."""
    cfg = _cfg_two_agents()
    reg = load_code_agents(cfg)
    from ack.tooling_envelope import load_tooling_envelope_policy
    monkeypatch.setattr(gx10, "TOOLING_ENVELOPE_POLICY", load_tooling_envelope_policy(cfg))
    monkeypatch.setattr(gx10, "_code_agent_registry", lambda: reg)
    monkeypatch.setattr(providers, "resolve_agent_bin",
                        lambda s: f"/usr/bin/{s.bin}" if s and s.agent_id in agents else None)
    # probe_code_agents uses resolve_agent_bin internally — keep them consistent
    monkeypatch.setattr(providers, "probe_code_agents",
                        lambda r: {a: (f"/usr/bin/{r.resolve(a).bin}" if a in agents else None)
                                   for a in r.names()})


# ── registration + ingestion membership ──────────────────────────────────────
def test_review_registered_as_ingestion_read(monkeypatch):
    _make_runnable(monkeypatch)
    names = {t["function"]["name"] for t in gx10._effective_tools()}
    assert "review" in names
    assert "review" in gx10._all_tool_names()
    assert "review" in gx10._INGESTION_TOOLS            # a READ of external reviewer text
    assert "review" not in gx10._AUDIT_TOOLS            # not a mutation
    assert "review" not in gx10.LOCAL_TOOL_NAMES        # server-side (CLI runner)
    assert "review" in gx10._TOOL_LABELS


def test_review_capability_gate_off_when_no_agent_bin(monkeypatch):
    reg = load_code_agents(_cfg_two_agents())
    monkeypatch.setattr(gx10, "_code_agent_registry", lambda: reg)
    monkeypatch.setattr(providers, "probe_code_agents", lambda r: {a: None for a in r.names()})
    monkeypatch.setattr(providers, "resolve_agent_bin", lambda s: None)
    assert gx10._review_available() is False
    assert "review" not in {t["function"]["name"] for t in gx10._effective_tools()}
    out = gx10.run_tool("review", {})
    assert out.startswith("ERROR: review is unavailable")


def test_review_capability_gate_on_when_agent_resolves(monkeypatch):
    _make_runnable(monkeypatch)
    assert gx10._review_available() is True


# ── agent selection + anti-affinity ──────────────────────────────────────────
def test_pick_reviewer_explicit_agent(monkeypatch):
    _make_runnable(monkeypatch)
    assert gx10._pick_reviewer("SONNET") == "SONNET"
    assert gx10._pick_reviewer("opus") == "OPUS"          # case-insensitive
    assert gx10._pick_reviewer("BOGUS") is None            # fail-closed


def test_pick_reviewer_config_default(monkeypatch):
    _make_runnable(monkeypatch)
    monkeypatch.setattr(gx10, "REVIEW_AGENT", "SONNET")
    monkeypatch.setattr(gx10, "_code_agent_pin", lambda: None)
    assert gx10._pick_reviewer(None) == "SONNET"


def test_pick_reviewer_anti_affinity_excludes_producer_pin(monkeypatch):
    # #457 SOFT: when the producer is pinned OPUS and a peer is runnable → pick the peer.
    _make_runnable(monkeypatch)
    monkeypatch.setattr(gx10, "REVIEW_AGENT", "")
    monkeypatch.setattr(gx10, "_code_agent_pin", lambda: "OPUS")
    assert gx10._pick_reviewer(None) == "SONNET"


def test_pick_reviewer_anti_affinity_waives_when_only_producer(monkeypatch):
    # Only OPUS is runnable and it is the producer → waive (keep OPUS), never empty.
    _make_runnable(monkeypatch, agents=("OPUS",))
    monkeypatch.setattr(gx10, "REVIEW_AGENT", "")
    monkeypatch.setattr(gx10, "_code_agent_pin", lambda: "OPUS")
    assert gx10._pick_reviewer(None) == "OPUS"


def test_pick_reviewer_config_equals_producer_picks_peer(monkeypatch):
    # Config review.agent == producer pin with a runnable peer → peer wins (no self-review).
    _make_runnable(monkeypatch)
    monkeypatch.setattr(gx10, "REVIEW_AGENT", "OPUS")
    monkeypatch.setattr(gx10, "_code_agent_pin", lambda: "OPUS")
    assert gx10._pick_reviewer(None) == "SONNET"


def test_pick_reviewer_config_equals_producer_sole_runnable_waives(monkeypatch):
    # Config review.agent == producer and no peer runnable → SOFT waive, keep producer.
    _make_runnable(monkeypatch, agents=("OPUS",))
    monkeypatch.setattr(gx10, "REVIEW_AGENT", "OPUS")
    monkeypatch.setattr(gx10, "_code_agent_pin", lambda: "OPUS")
    assert gx10._pick_reviewer(None) == "OPUS"


def test_review_tool_agent_enum_tracks_registry(monkeypatch):
    # #1221: REVIEW_TOOL's agent enum is LIVE from the registry (not hard-coded OPUS/SONNET).
    cfg = _cfg_two_agents()
    cfg["code_agents"]["pool"].append({
        "provider_id": "cli-codex", "kind": "cli", "agent_id": "CODEX",
        "model": "gpt-5.3-codex", "bin": "codex",
        "cmd_template": "{bin} --model {model} --print {prompt}", "effort": "high",
    })
    reg = load_code_agents(cfg)
    monkeypatch.setattr(gx10, "_code_agent_registry", lambda: reg)
    monkeypatch.setattr(providers, "resolve_agent_bin",
                        lambda s: f"/usr/bin/{s.bin}" if s else None)
    monkeypatch.setattr(providers, "probe_code_agents",
                        lambda r: {a: f"/usr/bin/{r.resolve(a).bin}" for a in r.names()})
    tools = {t["function"]["name"]: t for t in gx10._effective_tools()}
    assert "review" in tools
    enum = tools["review"]["function"]["parameters"]["properties"]["agent"]["enum"]
    assert "CODEX" in enum
    assert "OPUS" in enum and "SONNET" in enum


# ── runner invocation (mocked) ───────────────────────────────────────────────
def test_review_invokes_default_cli_runner_with_resolved_spec(monkeypatch):
    _make_runnable(monkeypatch)
    monkeypatch.setattr(gx10, "REVIEW_AGENT", "SONNET")
    monkeypatch.setattr(gx10, "REVIEW_TIMEOUT_S", 42.0)
    monkeypatch.setattr(gx10, "_assemble_review_material",
                        lambda paths: ("diff", "diff --git a/x b/x\n+hello"))
    captured = {}

    def fake_runner(spec, prompt, *, effort, max_tokens=None, timeout=None):
        captured["spec"] = spec
        captured["prompt"] = prompt
        captured["effort"] = effort
        captured["timeout"] = timeout
        return {"ok": True, "content": "## Summary\nok\n## Findings\n- [low] fine\n"
                                      "## Recommendations\n- ship\n## Verdict\nAPPROVE\n",
                "error": None, "completion_tokens": None, "latency": 0.1}

    import client
    monkeypatch.setattr(client, "default_cli_runner", fake_runner)
    out = gx10.run_tool("review", {"focus": "security", "agent": "SONNET"})
    assert out.startswith("[review by SONNET · diff]")
    assert "APPROVE" in out
    assert captured["spec"].agent_id == "SONNET"
    assert captured["timeout"] == 42.0
    assert "security" in captured["prompt"]
    assert "diff --git" in captured["prompt"]
    assert "independent cross-model reviewer" in captured["prompt"].lower() or \
           "INDEPENDENT" in captured["prompt"] or "independent" in captured["prompt"]


def test_review_unknown_agent_fails_closed_without_runner(monkeypatch):
    _make_runnable(monkeypatch)
    called = {"n": 0}

    def fake_runner(*a, **k):
        called["n"] += 1
        return {"ok": True, "content": "x"}

    import client
    monkeypatch.setattr(client, "default_cli_runner", fake_runner)
    out = gx10.run_tool("review", {"agent": "NOPE"})
    assert out.startswith("ERROR: review agent")
    assert called["n"] == 0


def test_review_runner_failure_surfaces_error(monkeypatch):
    _make_runnable(monkeypatch)
    monkeypatch.setattr(gx10, "_assemble_review_material",
                        lambda paths: ("diff", "+x"))
    import client
    monkeypatch.setattr(client, "default_cli_runner",
                        lambda *a, **k: {"ok": False, "content": None, "error": "TimeoutExpired()"})
    out = gx10.run_tool("review", {"agent": "SONNET"})
    assert out.startswith("ERROR: review by SONNET failed")
    assert "TimeoutExpired" in out


# ── material: diff-mode + paths-mode ─────────────────────────────────────────
def test_assemble_diff_mode_calls_git(monkeypatch):
    class _R:
        returncode = 0
        stdout = "diff --git a/f b/f\n+line"
        stderr = ""

    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _R()

    monkeypatch.setattr(gx10.subprocess, "run", fake_run)
    monkeypatch.setattr(gx10, "_exec_cwd", lambda: "/proj")
    mode, material = gx10._assemble_review_material(None)
    assert mode == "diff"
    assert "diff --git" in material
    assert captured["cmd"][:4] == ["git", "-C", "/proj", "diff"]


def test_assemble_paths_mode_reads_files(tmp_path, monkeypatch):
    doc = tmp_path / "decision.md"
    doc.write_text("# ADR\nUse adapter seam.\n", encoding="utf-8")
    monkeypatch.setattr(gx10, "_resolve_exec_path", lambda p: Path(p) if Path(p).is_absolute()
                        else tmp_path / p)
    mode, material = gx10._assemble_review_material(["decision.md"])
    assert mode == "paths"
    assert "ADR" in material and "adapter seam" in material
    assert "decision.md" in material


def test_assemble_paths_mode_all_missing_is_error(tmp_path, monkeypatch):
    monkeypatch.setattr(gx10, "_resolve_exec_path", lambda p: tmp_path / p)
    mode, material = gx10._assemble_review_material(["nope.md"])
    assert mode == "paths"
    assert material.startswith("ERROR: no readable files")


def test_review_paths_mode_end_to_end(tmp_path, monkeypatch):
    _make_runnable(monkeypatch)
    plan = tmp_path / "plan.md"
    plan.write_text("## Plan\nShip the review tool.\n", encoding="utf-8")
    monkeypatch.setattr(gx10, "_resolve_exec_path", lambda p: tmp_path / Path(p).name)
    import client
    monkeypatch.setattr(client, "default_cli_runner",
                        lambda spec, prompt, **kw: {
                            "ok": True,
                            "content": "## Summary\nplan ok\n## Findings\n- [low] none\n"
                                       "## Recommendations\n- proceed\n## Verdict\nAPPROVE\n",
                            "error": None,
                        })
    out = gx10.run_tool("review", {"paths": ["plan.md"], "focus": "completeness", "agent": "OPUS"})
    assert out.startswith("[review by OPUS · paths]")
    assert "APPROVE" in out


# ── config keys ──────────────────────────────────────────────────────────────
def test_review_config_defaults_present():
    d = gx10._code_defaults()
    assert "review" in d
    assert "agent" in d["review"] and "timeout_s" in d["review"]
    assert d["review"]["timeout_s"] == gx10.REVIEW_TIMEOUT_S


def test_apply_config_sets_review_globals(monkeypatch):
    cfg = gx10._code_defaults()
    cfg["review"] = {"agent": "sonnet", "timeout_s": 99}
    # _apply_config needs a full tree — use defaults and overlay review
    gx10._apply_config(cfg)
    assert gx10.REVIEW_AGENT == "SONNET"
    assert gx10.REVIEW_TIMEOUT_S == 99.0
