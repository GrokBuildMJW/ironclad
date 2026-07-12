"""#1227 (S5) — the fail-closed design→impl approval gate (no blind coding, R2/R3).

An IMPLEMENTATION stage_handover is REFUSED until the active unit has a recorded + APPROVED design; design/
analysis handovers pass through. `record_design` persists a proposal; `/approve` promotes it to a decision.
These tests drive the real `_stage_handover` path + the record_design→approve round-trip.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))
_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402


def _setup(monkeypatch, tmp_path, *, gate=True):
    gx10._apply_config(gx10._code_defaults())
    monkeypatch.setattr(gx10, "DESIGN_GATE_ENABLED", gate)   # the gate is opt-in (default OFF) — enable it here
    gx10.STORE = None
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    gx10.initiative_new("Demo", "software")


def test_gate_off_by_default_allows_impl(monkeypatch, tmp_path):
    # opt-in: with the gate DISABLED (the default), an implementation handover is byte-identical (allowed).
    _setup(monkeypatch, tmp_path, gate=False)
    out = _stage(_impl_json())                               # no design, but the gate is off
    assert "refused" not in out.lower()
    assert len(_pending()) == 1


def _impl_json(title="build it"):
    return json.dumps({"type": "implementation", "priority": "high", "title": title, "description": "x"})


def _stage(task_json):
    return gx10._stage_handover(None, "OPUS", "handover body", task_json)


def _pending():
    return gx10._store().list("pending")


def _design_frontmatter():
    doc = gx10.vault_root() / gx10.active_slug() / "decisions" / "design.md"
    return gx10._parse_frontmatter(doc.read_text(encoding="utf-8"))


# ── S3 (#1416 / ADR-0006 D5): proposal-variant + decision helpers ───────────────────────────────────
def _decision_doc():
    return gx10.vault_root() / gx10.active_slug() / "decisions" / "design.md"


def _proposals_dir():
    return gx10.vault_root() / gx10.active_slug() / "proposals"


def _proposal_doc(n: int = 1):
    return _proposals_dir() / f"design-{n}.md"


def _proposal_frontmatter(n: int = 1):
    return gx10._parse_frontmatter(_proposal_doc(n).read_text(encoding="utf-8"))


def _decision_frontmatter():
    return gx10._parse_frontmatter(_decision_doc().read_text(encoding="utf-8"))


def _proposal_files():
    d = _proposals_dir()
    return sorted(p.name for p in d.glob("design-*.md")) if d.is_dir() else []


def test_impl_refused_without_design(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    out = _stage(_impl_json())
    assert "blind-coding refused" in out
    assert _pending() == []                                  # fail-closed BEFORE store.create — nothing created


def test_impl_refused_with_unapproved_design(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_design("Approach", "use Rust")
    out = _stage(_impl_json())
    assert "NOT approved" in out
    assert "/approve" in out and "approved: true" not in out
    assert _pending() == []


def test_impl_allowed_with_approved_design(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_design("Approach", "use Rust")
    gx10._approve_design()
    out = _stage(_impl_json())
    assert "refused" not in out.lower() and "NOT approved" not in out
    assert len(_pending()) == 1                              # allowed → task created


def test_non_impl_unaffected(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    tj = json.dumps({"type": "architecture", "priority": "high", "title": "design it", "description": "x"})
    out = _stage(tj)
    assert "refused" not in out.lower()
    assert len(_pending()) == 1                              # design/analysis handover is NOT gated


def test_pure_rehandover_unaffected(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_design("Approach", "use Rust")
    gx10._approve_design()
    _stage(_impl_json())
    tid = _pending()[0]["id"]
    out = gx10._stage_handover(tid, "OPUS", "re-handover", None)   # task_json=None → not gated
    assert "refused" not in out.lower()


def test_force_does_not_bypass_gate(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    out = gx10._stage_handover(None, "OPUS", "body", _impl_json(), True, True)  # force=True
    assert "blind-coding refused" in out
    assert _pending() == []


def test_record_design_approve_roundtrip(monkeypatch, tmp_path):
    # ADR-0006 D5 (S3): record writes a non-destructive proposal VARIANT; /approve PROMOTES it to the decision.
    _setup(monkeypatch, tmp_path)
    slug = gx10.active_slug()
    assert gx10._unit_design_status(slug) == (False, False, None)
    rel = gx10.record_design("Approach", "use Rust")
    assert rel.endswith("proposals/design-1.md")             # variant under proposals/, not decisions/
    assert not _decision_doc().exists()                      # decisions/ empty pre-approve (purity)
    assert _proposal_frontmatter(1)["type"] == "proposal"
    assert _proposal_frontmatter(1)["approved"] == "false"
    hd, ap, ref = gx10._unit_design_status(slug)
    assert hd and not ap and ref.endswith("proposals/design-1.md")
    msg = gx10._approve_design()
    assert msg.startswith("OK")
    assert _decision_doc().is_file()                         # promoted into decisions/
    assert _decision_frontmatter()["type"] == "decision"
    assert _decision_frontmatter()["approved"] == "true"
    hd, ap, ref = gx10._unit_design_status(slug)
    assert hd and ap and ref.endswith("decisions/design.md")
    assert _proposal_doc(1).is_file()                        # proposal retained (variant provenance)


def test_approve_without_design(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    expected = (f"ERROR: unit {gx10.active_slug()!r} has no design to approve — record one first "
                f"(record_design). Nothing changed.")
    assert gx10._approve_design() == expected

    surfaced = []
    monkeypatch.setattr(gx10, "_ui_print", lambda message, *a, **k: surfaced.append(message))
    gx10._dispatch(None, "approve")
    assert len(surfaced) == 1 and expected in surfaced[0]       # `/approve` does not swallow the error


def test_steering_no_design_calls_record_design_now(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    block = gx10._steering_state_block()
    assert ("design gate: no design on record — implementation handovers are BLOCKED — if you have just "
            "researched/analysed a design, CALL record_design NOW to persist it (a prose proposal is not "
            "enough); then wait for /approve." in block)
    assert "recommend that the operator run `/design --options [N]`" in block


def test_steering_gate_off_has_no_design_gate_line(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=False)
    assert "design gate:" not in gx10._steering_state_block()   # default-off steering remains byte-identical
    assert "design options:" not in gx10._steering_state_block()


def test_record_design_non_destructive_after_approval(monkeypatch, tmp_path):
    # ADR-0006 D5 (S3, SUPERSEDES old test_record_design_resets_approval): recording a NEW approach after
    # approval is NON-destructive — it adds a proposal variant and leaves the approved decision INTACT (the
    # build stays on the decision until the operator promotes the new variant with `/approve design <id>`).
    _setup(monkeypatch, tmp_path)
    slug = gx10.active_slug()
    gx10.record_design("Approach", "use Rust")                 # -> proposals/design-1.md
    gx10._approve_design()                                     # promote -> decisions/design.md (approved)
    assert _decision_frontmatter()["approved"] == "true"
    assert gx10._unit_design_status(slug)[1] is True

    gx10.record_design("Auth redesign", "use Go instead")     # a 2nd variant — must NOT touch the decision
    assert _proposal_files() == ["design-1.md", "design-2.md"]  # both variants retained
    assert _proposal_frontmatter(2)["approved"] == "false"
    assert _decision_frontmatter()["approved"] == "true"      # decision UNCHANGED (non-destructive)
    assert gx10._unit_design_status(slug)[1] is True          # gate STAYS open on the approved decision
    assert gx10._design_gate("implementation", slug) is None  # implementation still allowed
    # The gate switches to the new variant only when the operator promotes it.
    msg = gx10._approve_command("design 2")
    assert msg.startswith("OK")
    assert "use Go instead" in _decision_doc().read_text(encoding="utf-8")  # decision now the 2nd variant


def test_record_design_no_approved_proposal_ever(monkeypatch, tmp_path):
    # decisions/ purity: a proposal is never approved-in-place; only the promoted decision is approved:true.
    _setup(monkeypatch, tmp_path)
    gx10.record_design("A", "x")
    gx10.record_design("B", "y")
    gx10._approve_command("design 1")
    for n in (1, 2):
        assert _proposal_frontmatter(n)["approved"] == "false"   # proposals stay approved:false
    assert _decision_frontmatter()["approved"] == "true"


def test_legacy_unapproved_decision_remains_pending(monkeypatch, tmp_path):
    # ADR-0006 D5 (S3): record_design now writes proposals/, so write the stray unapproved decisions/design.md
    # directly (legacy state). The gate must refuse impl until it is promoted (approved).
    _setup(monkeypatch, tmp_path)
    slug = gx10.active_slug()
    ddir = gx10.vault_root() / slug / "decisions"
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "design.md").write_text(
        "---\ntype: decision\nstage: design\napproved: false\ntitle: Legacy\n---\n\n# Legacy\n\nbody\n",
        encoding="utf-8", newline="\n")

    assert _decision_frontmatter()["type"] == "decision"
    assert _decision_frontmatter()["approved"] == "false"
    assert gx10._unit_design_status(slug)[1] is False
    out = _stage(_impl_json())
    assert "NOT approved" in out
    assert _pending() == []
    # /approve stamps the legacy stray in place (no proposal to promote).
    assert gx10._approve_design().startswith("OK")
    assert _decision_frontmatter()["approved"] == "true"


def test_rehandover_of_impl_task_gated_when_unapproved(monkeypatch, tmp_path):
    # Sonnet finding #3: re-handing an impl task (task_json=None) still RUNS the gate — no bypass.
    # ADR-0006 D5 (S3): record_design is non-destructive (no longer un-approves), so simulate an unapproved
    # decision directly (manual edit / legacy state) to prove the gate re-closes on the re-hand.
    _setup(monkeypatch, tmp_path)
    gx10.record_design("Approach", "use Rust")
    gx10._approve_design()
    _stage(_impl_json())                                      # impl task created (design approved)
    tid = _pending()[0]["id"]
    doc = _decision_doc()
    doc.write_text(gx10._set_frontmatter_flag(doc.read_text(encoding="utf-8"), "approved", "false"),
                   encoding="utf-8", newline="\n")            # decision becomes unapproved (manual/legacy)
    out = gx10._stage_handover(tid, "OPUS", "impl now", None)  # re-hand the impl task with no task_json
    assert "NOT approved" in out                              # refused — no bypass


def test_rehandover_unknown_task(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    out = gx10._stage_handover("KGC-999", "OPUS", "body", None)
    assert out.startswith("ERROR: no such task")


def test_design_gate_unit(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    slug = gx10.active_slug()
    assert gx10._design_gate("documentation", slug) is None                  # non-impl ungated
    assert gx10._design_gate("implementation", slug).startswith("ERROR")     # no design
    assert gx10._design_gate("implementation", None).startswith("ERROR")     # no unit


# ── #1346: design_gate.enabled uses strict _as_bool (string "false" must NOT enable) ───────────────
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        ("true", True),
        ("false", False),
        ("0", False),
        ("garbage", False),
        ("", False),
        (1, False),
    ],
)
def test_design_gate_config_uses_strict_boolean(value, expected):
    gx10._apply_design_gate({"design_gate": {"enabled": value}})
    assert gx10.DESIGN_GATE_ENABLED is expected


def test_design_gate_config_fails_soft(monkeypatch):
    monkeypatch.setattr(gx10, "_cfg_get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    gx10._apply_design_gate({})
    assert gx10.DESIGN_GATE_ENABLED is False


@pytest.mark.parametrize(
    ("fragment", "expected"),
    [
        ({"design_gate": {"enabled": True}}, True),
        ({"design_gate": {"enabled": "true"}}, True),
        ({"design_gate": {"enabled": "false"}}, False),  # strict _as_bool rejects stringy false
        ({"design_gate": {"enabled": "garbage"}}, False),
        ({"design_gate": {"enabled": "0"}}, False),
        ({}, False),  # missing key → public default off
    ],
    ids=["json-true", "string-true", "string-false", "garbage", "string-zero", "missing"],
)
def test_apply_config_design_gate_synthetic(fragment, expected):
    """#1346: synthetic dict only — string config values must not wrongly enable the gate."""
    cfg = gx10._code_defaults()
    cfg.update(fragment)
    gx10._apply_config(cfg)
    assert gx10.DESIGN_GATE_ENABLED is expected


# ── #1267: no duplicate H1 in the recorded design doc ───────────────────────────────────────────────
def _h1_count(md: str) -> int:
    return sum(1 for ln in md.splitlines() if ln.startswith("# "))


def _design_body(rel: str) -> str:
    # #1276: record_design now returns a project-root-relative (navigable) path → resolve it from the project
    # root (the test's chdir'd workdir), not vault_root (which would double the `vault/` prefix).
    base = gx10._project_root() or Path.cwd()
    text = (base / rel).read_text(encoding="utf-8")
    return text.split("---", 2)[2]                            # drop the leading frontmatter block


def test_record_design_no_duplicate_h1(monkeypatch, tmp_path):
    # #1267: when the body already opens with its own H1, record_design must NOT inject a second `# {title}`.
    _setup(monkeypatch, tmp_path)
    rel = gx10.record_design("FileSearch — Design", "# FileSearch CLI\n\nBody text.")
    body = _design_body(rel)
    assert _h1_count(body) == 1                               # exactly one top-level heading, not two
    assert "# FileSearch CLI" in body                         # the model's own heading is preserved


def test_record_design_injects_title_h1_when_body_has_none(monkeypatch, tmp_path):
    # #1267: a body without its own heading still gets the title as an H1 (unchanged for that case).
    _setup(monkeypatch, tmp_path)
    rel = gx10.record_design("MyTitle", "just prose, no heading")
    body = _design_body(rel)
    assert _h1_count(body) == 1 and "# MyTitle" in body


def test_record_design_returns_navigable_project_root_relative_path(monkeypatch, tmp_path):
    # #1276: the reported path is in the OPERATOR's frame — project-root-relative (leads with `vault/`), so it
    # resolves from where their shell runs, unlike the vault-root-relative value the gate uses internally.
    # ADR-0006 D5 (S3): under design_gate the recorded doc is the proposal variant.
    _setup(monkeypatch, tmp_path)
    rel = gx10.record_design("Approach", "use Rust")
    assert rel.startswith("vault/") and rel.endswith("proposals/design-1.md")
    base = gx10._project_root() or Path.cwd()
    assert (base / rel).is_file()                            # the reported path actually resolves on disk


# ── #1269: /approve confirms AND recommends the next step ────────────────────────────────────────────
def test_approve_message_includes_next_step(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_design("Approach", "use Rust")
    msg = gx10._approve_design()
    assert msg.startswith("OK")                              # still a success confirmation
    assert "Next:" in msg and "plan_units" in msg            # guided next-step present, not a dead end
    assert "/auto" in msg                                    # #1296: the drain/guided switch is named


# ── S3 (#1416 / ADR-0006 D5): non-destructive design variants + promote-by-id ────────────────────────
def test_record_design_writes_variant_not_decision(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_design("Approach", "use Rust")
    assert _proposal_files() == ["design-1.md"]              # variant present under proposals/
    assert not _decision_doc().exists()                      # decisions/ untouched pre-approve


def test_two_records_preserve_both_variants(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    r1 = gx10.record_design("Python approach", "use Python")
    r2 = gx10.record_design("Rust approach", "use Rust")
    assert r1.endswith("proposals/design-1.md")
    assert r2.endswith("proposals/design-2.md")
    assert _proposal_files() == ["design-1.md", "design-2.md"]  # 1st preserved, not overwritten
    assert "use Python" in _proposal_doc(1).read_text(encoding="utf-8")
    assert "use Rust" in _proposal_doc(2).read_text(encoding="utf-8")


def test_promote_by_id_via_helper_and_command(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_design("Python approach", "use Python")       # design-1
    gx10.record_design("Rust approach", "use Rust")           # design-2
    # helper form
    msg = gx10._approve_design(design_id="2")
    assert msg.startswith("OK")
    assert "use Rust" in _decision_doc().read_text(encoding="utf-8")
    assert _decision_frontmatter()["approved"] == "true"
    # command form ('design-2' spelling too) switches the decision to design-1
    msg2 = gx10._approve_command("design design-1")
    assert msg2.startswith("OK")
    assert "use Python" in _decision_doc().read_text(encoding="utf-8")


def test_bare_approve_with_multiple_proposals_is_pick_one(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_design("A", "x")
    gx10.record_design("B", "y")
    out = gx10._approve_design()
    assert out.startswith("ERROR") and "multiple design proposals" in out
    assert "design-1" in out and "design-2" in out
    assert not _decision_doc().exists()                      # nothing promoted


def test_bare_approve_with_single_proposal_promotes(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_design("Only", "the one approach")
    out = gx10._approve_command("design")
    assert out.startswith("OK")
    assert _decision_frontmatter()["approved"] == "true"


def test_approve_unknown_proposal_id(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_design("A", "x")
    out = gx10._approve_design(design_id="9")
    assert out.startswith("ERROR") and "no such design proposal" in out
    assert not _decision_doc().exists()                      # nothing changed


def test_already_approved_note_hints_switch(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_design("A", "x")
    gx10._approve_design()                                    # promote design-1
    gx10.record_design("B", "y")                             # add a 2nd variant (non-destructive)
    out = gx10._approve_design()                            # bare approve: decision already approved
    assert "already approved" in out
    assert "/approve design <id>" in out                    # switch hint surfaced
    assert "design-2" in out                                # the genuinely newer variant is named


def test_already_approved_hint_excludes_promoted_proposal(monkeypatch, tmp_path):
    # Finding #6: the switch hint names only GENUINELY newer variants — the already-promoted proposal (which
    # IS the current decision) is excluded, and a lone promoted proposal yields no misleading hint at all.
    _setup(monkeypatch, tmp_path)
    gx10.record_design("A", "x")                             # design-1
    gx10._approve_design()                                   # promote design-1 -> decision
    out1 = gx10._approve_design()                            # re-approve: no newer variant recorded
    assert "already approved" in out1
    assert "/approve design <id>" not in out1               # no hint — design-1 already IS the decision
    gx10.record_design("B", "y")                            # design-2 (genuinely newer)
    out2 = gx10._approve_design()
    assert "already approved" in out2
    assert "design-2" in out2 and "/approve design <id>" in out2
    assert "design-1" not in out2                           # the promoted proposal is excluded from the hint


def test_build_policy_section_preserved_on_promote(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    body = ("Use Python and the stdlib only.\n\n"
            "## Build policy\n\n"
            "- dependencies: stdlib only; no third-party packages\n"
            "- egress: none — the tool must not open a network socket\n")
    gx10.record_design("Approach", body)
    gx10._approve_design()
    decision = _decision_doc().read_text(encoding="utf-8")
    assert "## Build policy" in decision                     # the decided standard carries the policy section
    assert "stdlib only; no third-party packages" in decision
    assert "egress: none — the tool must not open a network socket" in decision


def test_typed_language_carried_onto_promoted_decision(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10.record_design("Approach", "use Python", language="python")
    assert _proposal_frontmatter(1)["language"] == "python"
    gx10._approve_design()
    assert _decision_frontmatter()["language"] == "python"   # typed metadata carried onto the decision
    assert _decision_frontmatter()["type"] == "decision"


def test_unit_design_status_transitions(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    slug = gx10.active_slug()
    assert gx10._unit_design_status(slug) == (False, False, None)          # none
    gx10.record_design("Approach", "use Rust")
    hd, ap, ref = gx10._unit_design_status(slug)
    assert hd and not ap and ref.endswith("proposals/design-1.md")        # proposal
    gx10._approve_design()
    hd, ap, ref = gx10._unit_design_status(slug)
    assert hd and ap and ref.endswith("decisions/design.md")              # decision


def test_flag_off_layout_byte_identical_no_proposals(monkeypatch, tmp_path):
    # Gate OFF (default): record writes the single decisions/design.md and NEVER creates proposals/design-*.md.
    _setup(monkeypatch, tmp_path, gate=False)
    rel = gx10.record_design("Approach", "use Rust")
    assert rel.endswith("decisions/design.md")
    assert _decision_doc().is_file()
    assert _proposal_files() == []                                          # no variant layout when off
    assert _decision_frontmatter()["type"] == "proposal"
    assert _decision_frontmatter()["approved"] == "false"
    gx10._approve_design()                                                  # OFF path stamps in place
    assert _decision_frontmatter()["approved"] == "true"
    assert _proposal_files() == []


# ── S5 (#1418 / ADR-0006 D3): operator-triggered proposal options with trade-offs ───────────────────
class _DesignOptionsAgent:
    def __init__(self):
        self.prompts = []

    def run(self, prompt):
        self.prompts.append(prompt)
        gx10.record_design(
            "Python approach",
            "# Python approach\n\nUse Python.\n\n## Trade-offs\n\nPros: simple.\nCons: slower.\n",
        )
        gx10.record_design(
            "Rust approach",
            "# Rust approach\n\nUse Rust.\n\n## Trade-offs\n\nPros: fast.\nCons: more complex.\n",
        )
        return "done"


class _OneDesignOptionsAgent:
    def __init__(self):
        self.prompts = []

    def run(self, prompt):
        self.prompts.append(prompt)
        gx10.record_design("Only approach", "# Only approach\n\nUse Python.\n")
        return "done"


def test_design_options_records_pickable_tradeoff_variants(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    agent = _DesignOptionsAgent()

    out = gx10._design_command(agent, "--options 2")

    assert out.startswith("OK")
    assert "recorded 2 of 2 design proposal variants" in out
    assert "under proposals/" in out
    assert "with trade-offs" not in out
    assert len(agent.prompts) == 1
    assert "Call the `record_design` tool exactly 2 times" in agent.prompts[0]
    assert _proposal_files() == ["design-1.md", "design-2.md"]
    for n in (1, 2):
        text = _proposal_doc(n).read_text(encoding="utf-8")
        assert "## Trade-offs" in text
        assert "Pros:" in text and "Cons:" in text

    msg = gx10._approve_command("design 2")
    assert msg.startswith("OK")
    decision = _decision_doc().read_text(encoding="utf-8")
    assert "Rust approach" in decision and "## Trade-offs" in decision


def test_design_options_defaults_to_two(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    assert gx10._parse_design_options_args("--options") == (2, None)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("--options 1", "requires N >= 2"),
        ("--options 9", "caps N at 8"),
        ("--options foo", "invalid design option count"),
        ("", "usage: /design --options [N]"),
    ],
)
def test_design_options_bad_args_refuse_before_model_turn(monkeypatch, tmp_path, raw, expected):
    _setup(monkeypatch, tmp_path)
    agent = _DesignOptionsAgent()

    out = gx10._design_command(agent, raw)

    assert expected in out
    assert agent.prompts == []
    assert not _decision_doc().exists()
    assert _proposal_files() == []


def test_design_options_refused_when_gate_off(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, gate=False)
    agent = _DesignOptionsAgent()

    out = gx10._design_command(agent, "--options 2")

    assert out.startswith("ERROR")
    assert agent.prompts == []
    assert not _decision_doc().exists()
    assert _proposal_files() == []


def test_design_options_refuses_without_active_unit(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    gx10._active_path().unlink()
    agent = _DesignOptionsAgent()

    out = gx10._design_command(agent, "--options 2")

    assert out.startswith("ERROR")
    assert "needs an active unit" in out
    assert agent.prompts == []
    assert not (gx10.vault_root() / "proposals").exists()


def test_design_options_refuses_without_agent(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    out = gx10._design_command(None, "--options 2")

    assert out.startswith("ERROR")
    assert "needs a running orchestrator agent" in out
    assert not _decision_doc().exists()
    assert _proposal_files() == []


def test_design_options_warns_when_model_records_fewer_than_requested(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    agent = _OneDesignOptionsAgent()

    out = gx10._design_command(agent, "--options 2")

    assert out.startswith("WARN")
    assert "recorded only 1 of 2 requested design variants under proposals/" in out
    assert "with trade-offs" not in out
    assert len(agent.prompts) == 1
    assert _proposal_files() == ["design-1.md"]


def test_design_options_dispatch_invokes_agent(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    agent = _DesignOptionsAgent()
    surfaced = []
    monkeypatch.setattr(gx10, "_ui_print", lambda message, *a, **k: surfaced.append(message))

    gx10._dispatch(agent, "design --options 2")

    assert _proposal_files() == ["design-1.md", "design-2.md"]
    assert any("recorded 2 of 2 design proposal variants" in str(s) for s in surfaced)


def test_flag_off_record_design_after_approval_relabels_decision(monkeypatch, tmp_path):
    # Gate OFF (default): the legacy single-doc layout self-heals on the next record_design write.
    _setup(monkeypatch, tmp_path, gate=False)
    gx10.record_design("Approach", "use Rust")
    gx10._approve_design()

    gx10.record_design("Revised approach", "use Python instead")

    text = _decision_doc().read_text(encoding="utf-8")
    fm = gx10._parse_frontmatter(text)
    assert fm["type"] == "proposal"
    assert fm["approved"] == "false"
    assert "use Python instead" in text
    assert _proposal_files() == []


def test_approve_design_refuses_legacy_unapproved_doc_plus_proposals(monkeypatch, tmp_path):
    # A vault can have a legacy unapproved decisions/design.md from gate OFF, then proposal variants after
    # flipping the gate ON. Bare /approve must not overwrite the legacy doc; the operator has to pick a
    # proposal id explicitly.
    _setup(monkeypatch, tmp_path, gate=False)
    gx10.record_design("Legacy", "legacy body")
    legacy_before = _decision_doc().read_text(encoding="utf-8")

    monkeypatch.setattr(gx10, "DESIGN_GATE_ENABLED", True)
    gx10.record_design("Variant", "proposal body")
    proposal_before = _proposal_doc(1).read_text(encoding="utf-8")

    out = gx10._approve_design()

    assert out.startswith("ERROR")
    assert "legacy unapproved design.md and 1 proposal(s)" in out
    assert "`/approve design <id>`" in out
    assert _decision_doc().read_text(encoding="utf-8") == legacy_before
    assert _proposal_doc(1).read_text(encoding="utf-8") == proposal_before


def test_off_path_approve_design_slug_targets_that_unit(monkeypatch, tmp_path):
    # Byte-identical-off regression: with design_gate OFF, `/approve design <slug>` keeps its LEGACY
    # positional-slug meaning — it approves THAT unit, not the active one (the S3 proposal-id
    # reinterpretation is design_gate-ON only).
    _setup(monkeypatch, tmp_path, gate=False)
    a = gx10.active_slug()
    b = gx10.initiative_new("Other unit", "software").slug                  # creates + activates "other-unit"
    gx10.initiative_use(a)                                                   # re-activate the first unit
    assert gx10.active_slug() == a
    gx10.record_design("B approach", "x", slug=b)                           # design on the NON-active unit B
    bdoc = gx10.vault_root() / b / "decisions" / "design.md"
    assert gx10._parse_frontmatter(bdoc.read_text(encoding="utf-8"))["approved"] == "false"

    out = gx10._approve_command(f"design {b}")                              # token is a SLUG on the OFF path
    assert out.startswith("OK")
    assert gx10._parse_frontmatter(bdoc.read_text(encoding="utf-8"))["approved"] == "true"  # B approved
    assert not (gx10.vault_root() / a / "decisions" / "design.md").exists()  # active A untouched


# ── S3 (#1416 / ADR-0006 D5): steering surfaces the proposal-variant state ───────────────────────────
def test_steering_multiple_proposals_surfaces_pick_one_hint(monkeypatch, tmp_path):
    # With >1 recorded proposal variants and none approved, the steering design-gate line surfaces the
    # pick-one hint so promotion is unambiguous (`/approve design <id>`).
    _setup(monkeypatch, tmp_path)
    gx10.record_design("A", "x")
    gx10.record_design("B", "y")
    block = gx10._steering_state_block()
    assert "design gate:" in block and "NOT approved" in block
    assert "2 proposal variants recorded (design-1, design-2)" in block    # variants named
    assert "`/approve design <id>` to promote one" in block                # the pick-one hint


def test_steering_single_proposal_has_no_pick_one_hint(monkeypatch, tmp_path):
    # A single recorded proposal → no ambiguity → no pick-one hint (bare /approve promotes it).
    _setup(monkeypatch, tmp_path)
    gx10.record_design("Only", "one approach")
    block = gx10._steering_state_block()
    assert "design gate:" in block and "NOT approved" in block
    assert "proposal variants recorded" not in block                        # no pick-one hint for one
