"""Tests for the S13b lifecycle-completeness DELIVER-leg gate (issue #632):
  - the pure transition->stage mapper (``lifecycle_projector.stage_for_payload``) over the REAL
    driver/deliver payload shapes + the None cases;
  - ``lifecycle_projector.project_transitions`` (with injected fakes AND with the real S13a primitives)
    incl. idempotent re-projection + fail-closed empty tree_sha;
  - the ``/lifecycle gate`` engine command end-to-end (a temp initiative + a temp hash-chained
    ledger.jsonl → projects + gates → READY; a missing-stage / tampered / bad-input ledger → BLOCKED),
    proving it is a functioning consumer, not a dead seam.

Mirrors test_evidence_projection.py's setup: stub openai, put core/engine on sys.path, drive via a real
ProjectContext + a real temp vault initiative.
"""
from __future__ import annotations

import hashlib
import json
import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import project_context as pc
from project_context import ProjectContext
import gx10
import lifecycle_projector as lp

TS = "abc123def456abc123def456"
OTHER = "fff000fff000fff000fff000"


# ── ledger fixture builder: mirrors scripts/devloop/ledger.append (hash chain) so the engine-side
#    reader is tested against the REAL on-disk format, without importing the private module. ─────────
def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(seq, prev, payload) -> str:
    return hashlib.sha256(f"{seq}|{prev}|{_canon(payload)}".encode("utf-8")).hexdigest()


def _write_ledger(path: Path, payloads) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines, prev = [], "GENESIS"
    for seq, payload in enumerate(payloads):
        h = _hash(seq, prev, payload)
        lines.append(_canon({"seq": seq, "prev_hash": prev, "payload": payload, "hash": h}))
        prev = h
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# Real driver/deliver payload shapes (matched from scripts/devprocess/driver.py + devloop/deliver.py).
def _gate_green(unit=632):
    return {"unit": unit, "src": "GATE", "dst": "GATE", "guard": "gate", "passed": True, "reasons": []}


def _gate_red(unit=632):
    return {"unit": unit, "src": "GATE", "dst": "GATE", "guard": "gate", "passed": False,
            "reasons": ["pytest red"]}


def _apply_leg(unit=632):
    return {"unit": unit, "src": "GATE", "dst": "GATE", "guard": "apply", "passed": True,
            "reasons": ["applied abc123 -> branch"]}


def _review_pass(unit=632):
    return {"unit": unit, "src": "GATE", "dst": "REVIEW", "guard": "review-evidence", "passed": True,
            "reasons": []}


def _review_fail(unit=632):
    return {"unit": unit, "src": "GATE", "dst": "REVIEW", "guard": "review-evidence", "passed": False,
            "reasons": ["no convergence"]}


def _review_inert(unit=632):
    # the run.py dry-run / non-live review shape (#830): a PASS, but marked `(inert)` — not real evidence.
    return {"unit": unit, "src": "GATE", "dst": "REVIEW", "guard": "review-evidence", "passed": True,
            "reasons": ["dry-run: review-evidence not enforced (inert)"]}


def _review_enforced(unit=632):
    # a live, enforced review-evidence leg carrying a real verdict reason (no `(inert)` marker).
    return {"unit": unit, "src": "GATE", "dst": "REVIEW", "guard": "review-evidence", "passed": True,
            "reasons": ["A<->B converged: 2 reviewers approved"]}


def _deliver(status):
    return {"surface": "DELIVER", "state": "DELIVER", "status": status, "reasons": []}


# ───────────────────────────── (a) stage_for_payload mapping ─────────────────────────────
def test_stage_for_payload_composed_gate_green_is_tests():
    assert lp.stage_for_payload(_gate_green()) == "tests"


def test_stage_for_payload_gate_red_is_none():
    assert lp.stage_for_payload(_gate_red()) is None


def test_stage_for_payload_apply_leg_is_not_tests():
    # the apply leg is also GATE->GATE/passed but guard == "apply" — must NOT map to tests
    assert lp.stage_for_payload(_apply_leg()) is None


def test_stage_for_payload_review_pass_is_reviews():
    assert lp.stage_for_payload(_review_pass()) == "reviews"


def test_stage_for_payload_review_fail_is_none():
    assert lp.stage_for_payload(_review_fail()) is None


def test_stage_for_payload_inert_review_is_none():
    # #830: a dry-run / non-live review passes but carries the `(inert)` marker — NOT reviews evidence.
    assert lp.stage_for_payload(_review_inert()) is None


def test_stage_for_payload_enforced_review_with_reasons_is_reviews():
    # #830: a live review with a real (non-inert) verdict reason still maps to `reviews`.
    assert lp.stage_for_payload(_review_enforced()) == "reviews"


@pytest.mark.parametrize("status", ["delivered", "delivered-pending", "delivered-unrecorded"])
def test_stage_for_payload_deliver_delivered_is_delivery(status):
    assert lp.stage_for_payload(_deliver(status)) == "delivery"


@pytest.mark.parametrize("status", ["halted-gate", "parked-awaiting-go", "halted-execute", "halted-error"])
def test_stage_for_payload_deliver_not_delivered_is_none(status):
    assert lp.stage_for_payload(_deliver(status)) is None


def test_stage_for_payload_coupling_transition_is_none():
    # a non-GATE/non-REVIEW green transition (e.g. READY->BRANCH) is not stage-bearing
    rec = {"unit": 632, "src": "READY", "dst": "BRANCH", "guard": "c0-present", "passed": True, "reasons": []}
    assert lp.stage_for_payload(rec) is None


def test_stage_for_payload_review_to_pr_is_none():
    # the REVIEW->PR leg has dst == "PR" (not REVIEW) — not the review-evidence pass
    rec = {"unit": 632, "src": "REVIEW", "dst": "PR", "guard": "pr-create", "passed": True, "reasons": []}
    assert lp.stage_for_payload(rec) is None


@pytest.mark.parametrize("garbage", [None, "x", 123, [], {}, {"surface": "DELIVER"}, {"passed": True}])
def test_stage_for_payload_garbage_is_none(garbage):
    assert lp.stage_for_payload(garbage) is None


# ───────────── (b) project_transitions with injected fakes (pure mapper path) ─────────────
def _fake_primitives():
    calls = []

    def fake_project_evidence(stage, title, body, *, tree_sha, content_hash=None, slug=None):
        calls.append({"stage": stage, "title": title, "body": body, "tree_sha": tree_sha, "slug": slug})
        return f"{slug}/evidence/{stage}.md"

    def fake_completeness(slug, *, required_stages, tree_sha):
        have = {c["stage"] for c in calls}
        missing = [s for s in required_stages if s not in have]
        return (not missing), [f"missing evidence for stage {s!r}" for s in missing]

    return calls, fake_project_evidence, fake_completeness


def test_project_transitions_fakes_ready_all_stages_present():
    calls, fpe, fc = _fake_primitives()
    records = [_gate_green(), _review_pass(), _deliver("delivered-pending"), _apply_leg()]
    res = lp.project_transitions(records, slug="demo", tree_sha=TS,
                                 required_stages=["tests", "reviews", "delivery"],
                                 project_evidence=fpe, lifecycle_completeness=fc)
    assert res["ready"] is True
    assert res["reasons"] == []
    assert {c["stage"] for c in calls} == {"tests", "reviews", "delivery"}
    # every projection bound to the delivery tree_sha (Fork E1)
    assert all(c["tree_sha"] == TS for c in calls)
    assert res["projected"] == sorted({"demo/evidence/tests.md", "demo/evidence/reviews.md",
                                       "demo/evidence/delivery.md"})


def test_project_transitions_fakes_missing_stage_blocked():
    calls, fpe, fc = _fake_primitives()
    res = lp.project_transitions([_gate_green()], slug="demo", tree_sha=TS,
                                 required_stages=["tests", "delivery"],
                                 project_evidence=fpe, lifecycle_completeness=fc)
    assert res["ready"] is False
    assert any("delivery" in r for r in res["reasons"])


def test_project_transitions_never_raises_on_empty_or_garbage():
    calls, fpe, fc = _fake_primitives()
    for records in ([], None, ["x", 123, None, {}, {"junk": 1}]):
        res = lp.project_transitions(records, slug="demo", tree_sha=TS, required_stages=["tests"],
                                     project_evidence=fpe, lifecycle_completeness=fc)
        assert res["projected"] == []           # nothing mappable → nothing projected


def test_project_transitions_accepts_full_ledger_records():
    # a full ledger record envelope ({"payload": {...}}) is normalized to its payload
    calls, fpe, fc = _fake_primitives()
    wrapped = [{"seq": 0, "prev_hash": "GENESIS", "payload": _gate_green(), "hash": "deadbeef"}]
    res = lp.project_transitions(wrapped, slug="demo", tree_sha=TS, required_stages=["tests"],
                                 project_evidence=fpe, lifecycle_completeness=fc)
    assert res["ready"] is True
    assert [c["stage"] for c in calls] == ["tests"]


# ─────────── (b)+(c)+(d) project_transitions with the REAL S13a primitives (integration) ───────────
def test_project_transitions_real_ready(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v = gx10.initiative_new("Demo", "software")
        res = lp.project_transitions(
            [_gate_green(), _review_pass(), _deliver("delivered")],
            slug=v.slug, tree_sha=TS, required_stages=["tests", "reviews", "delivery"],
            project_evidence=gx10.project_evidence, lifecycle_completeness=gx10.lifecycle_completeness)
        assert res["ready"] is True
        assert res["reasons"] == []
        assert len(res["projected"]) == 3
        assert len(list((v.path / "evidence").glob("*.md"))) == 3


def test_project_transitions_real_missing_stage_blocked(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v = gx10.initiative_new("Demo", "software")
        res = lp.project_transitions(
            [_gate_green(), _review_pass()],   # no delivery transition
            slug=v.slug, tree_sha=TS, required_stages=["tests", "reviews", "delivery"],
            project_evidence=gx10.project_evidence, lifecycle_completeness=gx10.lifecycle_completeness)
        assert res["ready"] is False
        assert any("delivery" in r for r in res["reasons"])


def test_project_transitions_real_idempotent(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v = gx10.initiative_new("Demo", "software")
        records = [_gate_green(), _gate_green(), _review_pass(), _deliver("delivered")]  # dup gate
        a = lp.project_transitions(records, slug=v.slug, tree_sha=TS,
                                   required_stages=["tests", "reviews", "delivery"],
                                   project_evidence=gx10.project_evidence,
                                   lifecycle_completeness=gx10.lifecycle_completeness)
        files_a = sorted(p.name for p in (v.path / "evidence").glob("*.md"))
        b = lp.project_transitions(records, slug=v.slug, tree_sha=TS,
                                   required_stages=["tests", "reviews", "delivery"],
                                   project_evidence=gx10.project_evidence,
                                   lifecycle_completeness=gx10.lifecycle_completeness)
        files_b = sorted(p.name for p in (v.path / "evidence").glob("*.md"))
        assert a == b                       # byte-identical result
        assert files_a == files_b           # no new files on re-projection
        assert len(files_a) == 3            # the duplicate gate green deduped into one evidence doc


def test_project_transitions_real_empty_tree_sha_fail_closed(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v = gx10.initiative_new("Demo", "software")
        res = lp.project_transitions(
            [_gate_green(), _review_pass(), _deliver("delivered")],
            slug=v.slug, tree_sha="", required_stages=["tests", "reviews", "delivery"],
            project_evidence=gx10.project_evidence, lifecycle_completeness=gx10.lifecycle_completeness)
        assert res["ready"] is False
        assert res["reasons"]               # a fail-closed reason ("no delivery tree_sha")
        assert res["projected"] == []       # nothing written for an unbound delivery
        assert not (v.path / "evidence").exists() or not list((v.path / "evidence").glob("*.md"))


# ───────────────────── (e) the /lifecycle gate command end-to-end ─────────────────────
def test_lifecycle_gate_command_ready(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v = gx10.initiative_new("Demo", "software")
        ledger = Path(str(tmp_path)) / ".devloop" / "ledger.jsonl"   # the DEFAULT location
        _write_ledger(ledger, [_gate_green(), _review_pass(), _deliver("delivered-pending")])
        out = gx10._lifecycle_command(f"gate --tree {TS}")           # default slug=active, default ledger
        assert "READY" in out
        assert "BLOCKED" not in out
        assert len(list((v.path / "evidence").glob("*.md"))) == 3


def test_lifecycle_gate_command_missing_stage_blocked(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        gx10.initiative_new("Demo", "software")
        ledger = Path(str(tmp_path)) / "led.jsonl"
        _write_ledger(ledger, [_gate_green(), _review_pass()])        # no delivery
        out = gx10._lifecycle_command(f"gate --tree {TS} --ledger {ledger.as_posix()}")
        assert "BLOCKED" in out
        assert "delivery" in out


def test_lifecycle_gate_command_explicit_stages_override(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        gx10.initiative_new("Demo", "software")
        ledger = Path(str(tmp_path)) / "led.jsonl"
        _write_ledger(ledger, [_gate_green(), _review_pass()])
        # Fork C: required stages overridable — tests,reviews are present → READY
        out = gx10._lifecycle_command(f"gate --tree {TS} --ledger {ledger.as_posix()} --stages tests,reviews")
        assert "READY" in out


def test_lifecycle_gate_command_usage_without_gate_subcommand(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        gx10.initiative_new("Demo", "software")
        assert gx10._lifecycle_command("").startswith("usage:")
        assert gx10._lifecycle_command("bogus").startswith("usage:")


def test_lifecycle_gate_command_no_tree_blocked(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        gx10.initiative_new("Demo", "software")
        out = gx10._lifecycle_command("gate")
        assert "BLOCKED" in out and "tree_sha" in out


def test_lifecycle_gate_command_no_slug_blocked(tmp_path, monkeypatch):
    # no active initiative + no --slug → fail-closed BLOCKED (never a silent pass)
    monkeypatch.chdir(tmp_path)
    assert pc.current() is None
    out = gx10._lifecycle_command(f"gate --tree {TS}")
    assert "BLOCKED" in out and "slug" in out


# ── #933: the --tree resolver (git HEAD tree default, fail-soft) ──────────────────────────────────────
def test_git_head_tree_resolves_in_a_repo():
    # the monorepo IS a git repo → the committed HEAD tree sha resolves (40 hex chars)
    sha = gx10._git_head_tree(Path(__file__).resolve().parents[3])
    assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)


def test_git_head_tree_failsoft_outside_a_repo(tmp_path):
    # a non-repo dir → "" so the BLOCKED-no-tree path still fires (never binds a bogus tree)
    assert gx10._git_head_tree(tmp_path) == ""


def test_lifecycle_gate_defaults_tree_to_head_when_omitted(tmp_path, monkeypatch):
    # #933: --tree omitted → the resolver supplies the HEAD tree → the tree check passes (blocks later on
    # the missing ledger, not on 'no delivery tree_sha')
    monkeypatch.setattr(gx10, "_git_head_tree", lambda root=None: "a" * 40)
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        gx10.initiative_new("Demo", "software")
        out = gx10._lifecycle_command("gate")            # no --tree
        assert "no delivery tree_sha" not in out         # the resolver default kicked in


def test_lifecycle_gate_explicit_tree_overrides_resolver(tmp_path, monkeypatch):
    # an explicit --tree wins even if the resolver would return "" (e.g. the operator's DELIVER-GO tree)
    monkeypatch.setattr(gx10, "_git_head_tree", lambda root=None: "")
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        gx10.initiative_new("Demo", "software")
        out = gx10._lifecycle_command(f"gate --tree {TS}")
        assert "no delivery tree_sha" not in out         # explicit --tree used despite empty resolver


def test_lifecycle_gate_command_tampered_ledger_blocked(tmp_path):
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        gx10.initiative_new("Demo", "software")
        ledger = Path(str(tmp_path)) / "led.jsonl"
        _write_ledger(ledger, [_gate_green(), _review_pass(), _deliver("delivered")])
        # tamper: flip a payload field WITHOUT recomputing the hash → chain break
        lines = ledger.read_text(encoding="utf-8").splitlines()
        lines[0] = lines[0].replace('"passed":true', '"passed":false')
        ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")
        out = gx10._lifecycle_command(f"gate --tree {TS} --ledger {ledger.as_posix()}")
        assert "BLOCKED" in out and "integrity" in out


def test_lifecycle_gate_command_missing_ledger_blocked(tmp_path):
    # a missing ledger is an empty ledger → no evidence → completeness BLOCKED (fail-closed, no crash)
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        gx10.initiative_new("Demo", "software")
        out = gx10._lifecycle_command(f"gate --tree {TS} --ledger {(tmp_path / 'nope.jsonl').as_posix()}")
        assert "BLOCKED" in out


# ───────────────────── (f) default = delivery-only (the production reality) + drift guards ───────────
def test_lifecycle_gate_command_default_delivery_only_ready(tmp_path):
    # In production only DELIVER records reach the ledger (the per-unit GATE/REVIEW log seam is a no-op,
    # tracked as the producer-log follow-up). So the DEFAULT gate (--stages delivery) must pass on a
    # ledger that carries ONLY a delivered record — proving the shipped default is functional today.
    with pc.use(ProjectContext("p", str(tmp_path), "ns")):
        v = gx10.initiative_new("Demo", "software")
        ledger = Path(str(tmp_path)) / ".devloop" / "ledger.jsonl"   # the DEFAULT location
        _write_ledger(ledger, [_deliver("delivered")])               # ONLY a delivery record
        out = gx10._lifecycle_command(f"gate --tree {TS}")           # default --stages = delivery
        assert "READY" in out and "BLOCKED" not in out
        assert len(list((v.path / "evidence").glob("*.md"))) == 1    # one delivery-stage evidence doc


def test_default_required_stages_is_delivery_only():
    # Fork C / producer-log reality: the shipped default is delivery-only (tests/reviews opt-in).
    assert tuple(gx10._LIFECYCLE_DEFAULT_STAGES) == ("delivery",)


def test_gate_review_guard_constants_match_real_wired_names():
    # The mapper keys on the guard names the REAL driver/run.py emit — `guards.compose("gate", …)` and
    # the GATE->REVIEW `GuardResult("review-evidence", …)`. Pin them so a drift in the mapper is caught.
    assert lp._GATE_GUARD == "gate"
    assert lp._REVIEW_GUARD == "review-evidence"


def test_engine_ledger_hash_matches_devprocess_ledger():
    # gx10 re-implements the ledger hash chain engine-side (boundary: core/ must not import scripts/dev*).
    # Pin it byte-identical to the real producer so chain verification can never silently diverge.
    # The producer lives in scripts/devprocess/ (private, monorepo-only — NOT part of the public export),
    # so this cross-check **skips** on an installed/clean-room tree where scripts/ is absent, matching the
    # sibling idiom (test_doc_audit / test_export_leak_guard). The engine-side impl keeps its own tests.
    import importlib.util
    led_path = Path(__file__).resolve().parents[3] / "scripts" / "devprocess" / "ledger.py"
    if not led_path.is_file():
        import pytest
        pytest.skip("scripts/devprocess/ledger.py absent — public export / clean-room tree, cross-check N/A")
    spec = importlib.util.spec_from_file_location("_devprocess_ledger_xcheck", led_path)
    led = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = led
    spec.loader.exec_module(led)
    payload = {"unit": 632, "src": "GATE", "dst": "GATE", "guard": "gate", "passed": True, "reasons": ["x"]}
    assert gx10._ledger_hash(3, "deadbeef", payload) == led._hash(3, "deadbeef", payload)
    assert gx10._ledger_canonical(payload) == led._canonical(payload)
