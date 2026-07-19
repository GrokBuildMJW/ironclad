"""#1614 — optional automatic code review at the completed-unit advance chokepoint."""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
import providers  # noqa: E402
from providers import load_code_agents  # noqa: E402


def _agents_cfg():
    return {"code_agents": {"pool": [
        {"provider_id": "claude-opus", "kind": "cli", "agent_id": "OPUS",
         "model": "claude-opus-4-8", "bin": "claude",
         "cmd_template": "{bin} --model {model} --print {prompt}", "effort": "xhigh",
         "permission_mode": "bypassPermissions", "capabilities": {"permission_bypass": True}},
        {"provider_id": "claude-sonnet", "kind": "cli", "agent_id": "SONNET",
         "model": "claude-sonnet-5", "bin": "claude",
         "cmd_template": "{bin} --model {model} --print {prompt}", "effort": "high",
         "permission_mode": "bypassPermissions", "capabilities": {"permission_bypass": True}},
        {"provider_id": "cli-codex", "kind": "cli", "agent_id": "CODEX",
         "model": "gpt-5.3-codex", "bin": "codex",
         "cmd_template": "{bin} --model {model} --print {prompt}", "effort": "high",
         "permission_mode": "bypassPermissions", "capabilities": {"permission_bypass": True}},
    ]}}


def _make_runnable(monkeypatch, agents=("OPUS", "SONNET")):
    cfg = _agents_cfg()
    reg = load_code_agents(cfg)
    from ack.tooling_envelope import load_tooling_envelope_policy
    monkeypatch.setattr(gx10, "TOOLING_ENVELOPE_POLICY", load_tooling_envelope_policy(cfg))
    monkeypatch.setattr(gx10, "_code_agent_registry", lambda: reg)
    monkeypatch.setattr(providers, "resolve_agent_bin",
                        lambda spec: f"/usr/bin/{spec.bin}" if spec and spec.agent_id in agents else None)
    monkeypatch.setattr(providers, "probe_code_agents",
                        lambda registry: {
                            aid: (f"/usr/bin/{registry.resolve(aid).bin}" if aid in agents else None)
                            for aid in registry.names()
                        })


def _setup(monkeypatch, tmp_path, *, mode="off", max_rounds=2, agents=("OPUS", "SONNET")):
    cfg = gx10._code_defaults()
    cfg["code_review"] = {"mode": mode, "max_rounds": max_rounds, "agent": ""}
    gx10._apply_config(cfg)
    gx10.STORE = None
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    monkeypatch.setattr(gx10, "_mandatory_staging_gates", lambda *a, **k: None)
    monkeypatch.setattr(gx10, "_design_gate", lambda *a, **k: None)
    monkeypatch.setattr(gx10, "_design_build_check", lambda *a, **k: None)
    monkeypatch.setattr(gx10, "_assemble_review_material", lambda paths: ("diff", "diff --git a/x b/x\n+change"))
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Demo", "software")
    _make_runnable(monkeypatch, agents)


def _staged(*, agent="OPUS"):
    store = gx10._store()
    tid = store.create(
        {"type": "feature", "priority": "high", "title": "x", "description": "y"}, force=True)["id"]
    handover = gx10.handovers_dir() / f"{tid}_{agent}.md"
    handover.parent.mkdir(parents=True, exist_ok=True)
    handover.write_text(f"---\nto: {agent}\ntask_id: {tid}\n---\n\nImplement the unit.\n", encoding="utf-8")
    store.transition(tid, "in_progress")
    feedback = gx10.feedback_dir() / f"{tid}_{agent}-feedback.md"
    feedback.parent.mkdir(parents=True, exist_ok=True)
    feedback.write_text("---\nstatus: done\n---\n\nImplementation complete.\n", encoding="utf-8")
    return tid, feedback


def _review(verdict):
    return ("## Summary\nReviewed.\n## Findings\n- [severity: low] finding — x\n"
            "## Recommendations\n- act\n## Verdict\n" + verdict + "\n")


def _runner_result(content, *, ok=True):
    return {"ok": ok, "content": content if ok else None, "error": None if ok else content}


def test_code_review_off_is_noop(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, mode="off")
    tid, _feedback = _staged()
    import client
    monkeypatch.setattr(client, "default_cli_runner",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("reviewer must not run")))

    out = gx10._advance_pipeline(tid, "OPUS")

    assert out.startswith("OK: pipeline advanced")
    assert gx10._store().get(tid)["status"] == "done"
    assert not list(gx10._reviews_dir().glob("*.md"))


def test_code_review_skips_with_fewer_than_two_bound_coders(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, mode="simple", agents=("OPUS",))
    tid, _feedback = _staged()
    import client
    monkeypatch.setattr(client, "default_cli_runner",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("reviewer must not run")))

    out = gx10._advance_pipeline(tid, "OPUS")

    assert out.startswith("OK: pipeline advanced")
    assert gx10._store().get(tid)["status"] == "done"


def test_simple_review_approve_advances_and_writes_artifact(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, mode="simple")
    tid, _feedback = _staged()
    import client
    monkeypatch.setattr(client, "default_cli_runner",
                        lambda *a, **k: _runner_result(_review("APPROVE")))

    out = gx10._advance_pipeline(tid, "OPUS")

    assert out.startswith("OK: pipeline advanced")
    assert gx10._store().get(tid)["status"] == "done"
    artifact = gx10._reviews_dir() / f"{tid}_review_SONNET_r1.md"
    assert artifact.exists()
    assert "stage: reviews" in artifact.read_text(encoding="utf-8")


def test_simple_review_rejection_blocks_and_rehands(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, mode="simple")
    tid, feedback = _staged()
    import client
    monkeypatch.setattr(client, "default_cli_runner",
                        lambda *a, **k: _runner_result(_review("REQUEST_CHANGES")))

    out = gx10._advance_pipeline(tid, "OPUS")

    task = gx10._store().get(tid)
    assert out.startswith("BLOCKED:")
    assert task["status"] == "pending" and task["review_rounds"] == 1
    assert not feedback.exists()
    handover = (gx10.handovers_dir() / f"{tid}_OPUS.md").read_text(encoding="utf-8")
    assert gx10._CODE_REVIEW_FINDINGS_MARKER in handover
    assert "REQUEST_CHANGES" in handover and "fix these findings" in handover.lower()


def test_simple_review_max_rounds_marks_terminal_block(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, mode="simple", max_rounds=2)
    tid, feedback = _staged()
    task_path, _status = gx10._store()._find(tid)
    data = json.loads(task_path.read_text(encoding="utf-8"))
    data["review_rounds"] = 1
    task_path.write_text(json.dumps(data), encoding="utf-8")
    import client
    monkeypatch.setattr(client, "default_cli_runner",
                        lambda *a, **k: _runner_result(_review("REQUEST_CHANGES")))

    out = gx10._advance_pipeline(tid, "OPUS")

    task = gx10._store().get(tid)
    assert out.startswith("ERROR: code-review rejected (max rounds")
    assert task["status"] == "in_progress"
    assert task["blocked"] is True and task["blocked_kind"] == "review_rejected"
    assert task["review_rounds"] == 2      # terminal block persists the true round count (no off-by-one)
    assert not feedback.exists()


def test_meta_review_rejection_blocks_with_distinct_reviewers(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, mode="review_of_review", agents=("OPUS", "SONNET", "CODEX"))
    tid, _feedback = _staged()
    seen = []

    def runner(spec, prompt, **kwargs):
        seen.append(spec.agent_id)
        verdict = "APPROVE" if spec.agent_id == "SONNET" else "REQUEST_CHANGES"
        return _runner_result(_review(verdict))

    import client
    monkeypatch.setattr(client, "default_cli_runner", runner)

    out = gx10._advance_pipeline(tid, "OPUS")

    assert out.startswith("BLOCKED:")
    assert seen == ["SONNET", "CODEX"]
    assert (gx10._reviews_dir() / f"{tid}_review_SONNET_r1.md").exists()
    assert (gx10._reviews_dir() / f"{tid}_metareview_CODEX_r1.md").exists()


def test_code_review_anti_affinity_excludes_producer(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, mode="simple")
    monkeypatch.setattr(gx10, "_code_agent_pin", lambda: "OPUS")
    tid, _feedback = _staged(agent="OPUS")
    seen = []

    def runner(spec, prompt, **kwargs):
        seen.append(spec.agent_id)
        return _runner_result(_review("APPROVE"))

    import client
    monkeypatch.setattr(client, "default_cli_runner", runner)

    assert gx10._advance_pipeline(tid, "OPUS").startswith("OK: pipeline advanced")
    assert seen == ["SONNET"]


def test_reviewer_error_is_fail_soft_and_writes_note(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, mode="simple")
    tid, _feedback = _staged()
    import client
    monkeypatch.setattr(client, "default_cli_runner",
                        lambda *a, **k: _runner_result("TimeoutExpired()", ok=False))

    out = gx10._advance_pipeline(tid, "OPUS")

    assert out.startswith("OK: pipeline advanced")
    assert gx10._store().get(tid)["status"] == "done"
    artifact = gx10._reviews_dir() / f"{tid}_review_SONNET_r1.md"
    assert "verdict: reviewer_error" in artifact.read_text(encoding="utf-8")
    assert "TimeoutExpired" in artifact.read_text(encoding="utf-8")


def _review_raw(verdict_block: str) -> str:
    """A review whose Verdict section is arbitrary text (to exercise the tolerant/failing parser)."""
    return ("## Summary\nReviewed.\n## Findings\n- [severity: low] finding — x\n"
            "## Recommendations\n- act\n## Verdict\n" + verdict_block + "\n")


def test_review_material_error_skips_fail_soft(monkeypatch, tmp_path):
    # #1614 fix B: a failed diff assembly must not be fed to the reviewer as the diff — skip + advance.
    _setup(monkeypatch, tmp_path, mode="simple")
    monkeypatch.setattr(gx10, "_assemble_review_material",
                        lambda paths: ("diff", "ERROR: git diff failed: not a repository"))
    tid, _feedback = _staged()
    import client
    monkeypatch.setattr(client, "default_cli_runner",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("reviewer must not run")))

    out = gx10._advance_pipeline(tid, "OPUS")

    assert out.startswith("OK: pipeline advanced")
    assert gx10._store().get(tid)["status"] == "done"
    assert not list(gx10._reviews_dir().glob("*.md"))


def test_decorated_approve_verdict_advances(monkeypatch, tmp_path):
    # #1614 fix D: a genuine APPROVE with ordinary markdown decoration must parse and advance,
    # not false-block a completed unit.
    for i, block in enumerate(("**APPROVE**", "APPROVE", "- APPROVE", "> APPROVE")):
        sub = tmp_path / f"c{i}"
        sub.mkdir()
        _setup(monkeypatch, sub, mode="simple")
        tid, _feedback = _staged()
        import client
        monkeypatch.setattr(client, "default_cli_runner",
                            lambda *a, _b=block, **k: _runner_result(_review_raw(_b)))

        out = gx10._advance_pipeline(tid, "OPUS")

        assert out.startswith("OK: pipeline advanced"), block
        assert gx10._store().get(tid)["status"] == "done", block


def test_inline_colon_approve_verdict_advances(monkeypatch, tmp_path):
    # #1614 fix D: '## Verdict: APPROVE' (inline colon, common CLI-reviewer format) must parse.
    _setup(monkeypatch, tmp_path, mode="simple")
    tid, _feedback = _staged()
    review = ("## Summary\nok\n## Findings\n- none\n## Recommendations\n- none\n"
              "## Verdict: APPROVE\n")
    import client
    monkeypatch.setattr(client, "default_cli_runner", lambda *a, **k: _runner_result(review))

    out = gx10._advance_pipeline(tid, "OPUS")

    assert out.startswith("OK: pipeline advanced")
    assert gx10._store().get(tid)["status"] == "done"


def test_unparseable_verdict_advances_fail_soft(monkeypatch, tmp_path):
    # #1614 fix D: a reviewer that RAN but emitted no parseable verdict is fail-soft (advance), matching
    # the reviewer-error path — never fail-CLOSED against a completed unit on a formatting hiccup.
    _setup(monkeypatch, tmp_path, mode="simple")
    tid, _feedback = _staged()
    import client
    monkeypatch.setattr(client, "default_cli_runner",
                        lambda *a, **k: _runner_result(_review_raw("LGTM, ship it")))

    out = gx10._advance_pipeline(tid, "OPUS")

    assert out.startswith("OK: pipeline advanced")
    assert gx10._store().get(tid)["status"] == "done"
    artifact = gx10._reviews_dir() / f"{tid}_review_SONNET_r1.md"
    assert "verdict: unknown" in artifact.read_text(encoding="utf-8")


def test_rejection_survives_restaging_failure(monkeypatch, tmp_path):
    # #1614 fix A: if the re-handover staging fails AFTER a rejection is decided, the checkpoint must fail
    # CLOSED — return an explicit error, PRESERVE the done-feedback trigger, and NOT advance the unit.
    _setup(monkeypatch, tmp_path, mode="simple")
    tid, feedback = _staged()
    monkeypatch.setattr(gx10, "_stage_handover", lambda *a, **k: "ERROR: staging refused")
    import client
    monkeypatch.setattr(client, "default_cli_runner",
                        lambda *a, **k: _runner_result(_review("REQUEST_CHANGES")))

    out = gx10._advance_pipeline(tid, "OPUS")

    task = gx10._store().get(tid)
    assert out.startswith("ERROR: code-review rejected") and "staging failed" in out
    assert task["status"] == "in_progress"          # NOT advanced to done, NOT transitioned to pending
    assert task["review_rounds"] == 1               # persisted before the failed stage
    assert feedback.exists()                         # trigger preserved for a clean retry (fb NOT consumed)


def test_meta_review_double_approve_advances(monkeypatch, tmp_path):
    # #1614 fix F: review_of_review happy path — both reviewers APPROVE -> advance + both artifacts.
    _setup(monkeypatch, tmp_path, mode="review_of_review", agents=("OPUS", "SONNET", "CODEX"))
    tid, _feedback = _staged()
    seen = []

    def runner(spec, prompt, **kwargs):
        seen.append(spec.agent_id)
        return _runner_result(_review("APPROVE"))

    import client
    monkeypatch.setattr(client, "default_cli_runner", runner)

    out = gx10._advance_pipeline(tid, "OPUS")

    assert out.startswith("OK: pipeline advanced")
    assert gx10._store().get(tid)["status"] == "done"
    assert seen == ["SONNET", "CODEX"]
    assert (gx10._reviews_dir() / f"{tid}_review_SONNET_r1.md").exists()
    assert (gx10._reviews_dir() / f"{tid}_metareview_CODEX_r1.md").exists()


def test_review_of_review_degrades_to_simple_and_advances(monkeypatch, tmp_path):
    # #1614 fix G: review_of_review with only 2 runnable coders degrades to simple — reviewer_a APPROVE
    # advances with NO meta-review artifact and only one reviewer call.
    _setup(monkeypatch, tmp_path, mode="review_of_review", agents=("OPUS", "SONNET"))
    tid, _feedback = _staged()
    seen = []

    def runner(spec, prompt, **kwargs):
        seen.append(spec.agent_id)
        return _runner_result(_review("APPROVE"))

    import client
    monkeypatch.setattr(client, "default_cli_runner", runner)

    out = gx10._advance_pipeline(tid, "OPUS")

    assert out.startswith("OK: pipeline advanced")
    assert gx10._store().get(tid)["status"] == "done"
    assert seen == ["SONNET"]
    assert (gx10._reviews_dir() / f"{tid}_review_SONNET_r1.md").exists()
    assert not list(gx10._reviews_dir().glob("*_metareview_*.md"))


def test_review_of_review_degrades_to_simple_and_blocks(monkeypatch, tmp_path):
    # #1614 fix G: with 2 coders, a reviewer_a rejection blocks on reviewer_a alone (no meta-review).
    _setup(monkeypatch, tmp_path, mode="review_of_review", agents=("OPUS", "SONNET"))
    tid, feedback = _staged()
    seen = []

    def runner(spec, prompt, **kwargs):
        seen.append(spec.agent_id)
        return _runner_result(_review("REQUEST_CHANGES"))

    import client
    monkeypatch.setattr(client, "default_cli_runner", runner)

    out = gx10._advance_pipeline(tid, "OPUS")

    assert out.startswith("BLOCKED:")
    assert seen == ["SONNET"]
    assert gx10._store().get(tid)["review_rounds"] == 1
    assert not list(gx10._reviews_dir().glob("*_metareview_*.md"))
    assert not feedback.exists()


def test_restaging_strips_prior_findings_block_no_duplicate(monkeypatch, tmp_path):
    # #1614 fix H: across TWO reject rounds (max_rounds=3) the findings block is stripped-then-re-added,
    # so the handover carries exactly one findings block — never a stale duplicate.
    _setup(monkeypatch, tmp_path, mode="simple", max_rounds=3)
    tid, _feedback = _staged()
    import client
    monkeypatch.setattr(client, "default_cli_runner",
                        lambda *a, **k: _runner_result(_review("REQUEST_CHANGES")))

    assert gx10._advance_pipeline(tid, "OPUS").startswith("BLOCKED:")   # round 1 -> re-hand
    # the coder "re-emits" a done feedback to trigger the next advance round
    fb2 = gx10.feedback_dir() / f"{tid}_OPUS-feedback.md"
    fb2.parent.mkdir(parents=True, exist_ok=True)
    fb2.write_text("---\nstatus: done\n---\n\nFixed round 1.\n", encoding="utf-8")
    gx10._store().transition(tid, "in_progress")

    assert gx10._advance_pipeline(tid, "OPUS").startswith("BLOCKED:")   # round 2 -> re-hand again

    handover = (gx10.handovers_dir() / f"{tid}_OPUS.md").read_text(encoding="utf-8")
    assert handover.count(gx10._CODE_REVIEW_FINDINGS_MARKER) == 1
    assert handover.count(gx10._CODE_REVIEW_FINDINGS_END) == 1
    assert gx10._store().get(tid)["review_rounds"] == 2
