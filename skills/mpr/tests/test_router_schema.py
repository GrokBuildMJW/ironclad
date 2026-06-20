"""MPR router schema (skills/mpr/schema.py) — the pydantic SSOT (Spec 04 §2 + §3.5).

Pure schema/validation tests (no LLM, no registry, no network): the input contract, the decision
object's coherence rules (decline needs a reason; run needs route/domain/mode + a panel at/above the
hard floor), the panel-entry defaults, JSON round-trip (replay foundation), and the byte-alignment of
the enum *values* with the P0 provider-router so a MPR Perspective hands off to P0 by string.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from mpr.schema import (
    Decision,
    Effort,
    EvidenceSource,
    FileRef,
    Mode,
    Perspective,
    ProviderPolicy,
    Route,
    RouterDecision,
    RouterInput,
    _PANEL_HARD_FLOOR,
)


# ── Input contract (§2) ──────────────────────────────────────────────────────────────────────────
def test_router_input_minimal_valid():
    inp = RouterInput(query="Should we move from a modulith to microservices?")
    assert inp.route_hint is None
    assert inp.files == []
    assert inp.locale is None


def test_router_input_query_required_nonempty():
    with pytest.raises(ValidationError):
        RouterInput(query="")


def test_router_input_rejects_unknown_field():
    with pytest.raises(ValidationError):
        RouterInput(query="x", bogus=1)


def test_router_input_route_hint_literal_enforced():
    RouterInput(query="x", route_hint="wide")  # valid member
    with pytest.raises(ValidationError):
        RouterInput(query="x", route_hint="sideways")  # not in the Literal set


def test_file_ref_excerpt_only_no_full_body_field():
    f = FileRef(path="a/b.py", sha256="deadbeef", excerpt="head…", bytes=1234)
    assert f.path == "a/b.py"
    # the contract carries a reference + excerpt, never a full-content field
    assert "content" not in FileRef.model_fields and "body" not in FileRef.model_fields
    with pytest.raises(ValidationError):
        FileRef(path="a", content="whole file")  # extra=forbid


def test_router_input_files_coerced_from_dicts():
    inp = RouterInput(query="compare X and Y", files=[{"path": "a.py", "excerpt": "…"}])
    assert isinstance(inp.files[0], FileRef)
    assert inp.files[0].path == "a.py"


# ── Decision coherence (§3.5) ────────────────────────────────────────────────────────────────────
def _panel(n: int) -> list[Perspective]:
    return [Perspective(role=f"role-{i}", lens_prompt=f"lens {i}") for i in range(n)]


def test_decline_requires_reason():
    with pytest.raises(ValidationError, match="decline requires decline_reason"):
        RouterDecision(decision=Decision.DECLINE)


def test_decline_minimal_valid_no_panel():
    d = RouterDecision(decision=Decision.DECLINE, decline_reason="single-fact lookup")
    assert d.route is None and d.domain is None and d.perspectives == []
    assert d.decline_reason == "single-fact lookup"


def test_run_requires_route_domain_mode_panel():
    with pytest.raises(ValidationError, match="run requires route"):
        RouterDecision(decision=Decision.RUN, perspectives=_panel(3))  # no route/domain/mode


def test_run_below_hard_floor_rejected():
    assert _PANEL_HARD_FLOOR == 2
    with pytest.raises(ValidationError, match="hard floor"):
        RouterDecision(
            decision=Decision.RUN, route=Route.WIDE, domain="architecture-decision",
            mode=Mode.DECISION, perspectives=_panel(1),
        )


def test_run_at_hard_floor_valid():
    d = RouterDecision(
        decision=Decision.RUN, route=Route.WIDE, domain="architecture-decision",
        mode=Mode.DECISION, perspectives=_panel(_PANEL_HARD_FLOOR),
    )
    assert len(d.perspectives) == 2
    assert d.schema_version == "mpr.router/1"


def test_router_decision_rejects_unknown_field():
    with pytest.raises(ValidationError):
        RouterDecision(decision=Decision.DECLINE, decline_reason="x", bogus=True)


# ── Perspective defaults (§3.5) ──────────────────────────────────────────────────────────────────
def test_perspective_defaults():
    p = Perspective(role="SRE/Ops", lens_prompt="operate it at 3am")
    assert p.effort == Effort.MEDIUM
    assert p.provider_policy == ProviderPolicy.OFFLOADABLE


def test_perspective_role_and_lens_required_nonempty():
    with pytest.raises(ValidationError):
        Perspective(role="", lens_prompt="x")
    with pytest.raises(ValidationError):
        Perspective(role="x", lens_prompt="")


# ── Replay foundation: JSON round-trip is stable ─────────────────────────────────────────────────
def test_run_decision_json_round_trip_stable():
    d = RouterDecision(
        decision=Decision.RUN, route=Route.WIDE, domain="architecture-decision",
        mode=Mode.DECISION, perspectives=_panel(3), synthesis_template="decision-matrix",
        evidence_source=EvidenceSource.INTERNAL, classifier_raw='{"x":1}',
        guards_applied=["distinctness:dropped(foo)"],
    )
    dumped = d.model_dump_json()
    again = RouterDecision.model_validate_json(dumped)
    assert again.model_dump_json() == dumped  # byte-stable → safe to snapshot/replay


# ── Enum values are byte-aligned with the P0 provider-router (MPR → P0 handoff) ───────────────────
def test_provider_policy_values_match_p0_router():
    # engine/router.py is on sys.path via conftest; MPR hands provider_policy to P0 by *value*.
    from router import ProviderPolicy as P0Policy  # noqa: E402

    assert {e.value for e in ProviderPolicy} == {e.value for e in P0Policy}
    assert ProviderPolicy.LOCAL_ONLY.value == "local-only"
    assert ProviderPolicy.OFFLOADABLE.value == "offloadable"


def test_effort_values_match_p0_effort_rank():
    from router import EFFORT_RANK  # noqa: E402

    assert {e.value for e in Effort} == set(EFFORT_RANK)  # low|medium|high|xhigh


def test_evidence_and_route_enum_values():
    assert {e.value for e in EvidenceSource} == {"internal", "external", "mixed"}
    assert {e.value for e in Route} == {"wide", "focused", "file-only", "file-augmented"}
    assert {e.value for e in Mode} == {"decision", "evidence-research", "comparison"}
