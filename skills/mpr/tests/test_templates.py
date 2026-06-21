"""MPR output templates (skills/mpr/templates/) — Spec 06 §4 / §8-C. Deterministic, LLM-free.

MPR computes the weighted sum itself (LLM number ignored); fallback==recommendation flags invalid but
still renders; blocking conflicts are always visible; evidence tiers demote conservatively; an
unparsable body degrades to invalid while preserving the conflict zones.
"""
from __future__ import annotations

import json

from mpr.conflicts import Conflict, ConflictSide
from mpr.templates import resolve_tier, validate_template
from mpr.templates.evidence import EvidenceReport, Finding


def _decision_body(rec="B", fallback="A", wrong_sum=999):
    return json.dumps({
        "options": ["A", "B"],
        "criteria": [{"name": "Wartbarkeit", "weight": 2}, {"name": "Kosten", "weight": 3}],
        "cells": [
            {"option": "A", "criterion": "Wartbarkeit", "score": 4},
            {"option": "A", "criterion": "Kosten", "score": 2},
            {"option": "B", "criterion": "Wartbarkeit", "score": 1},
            {"option": "B", "criterion": "Kosten", "score": 5},
        ],
        "recommendation": rec, "recommendation_rationale": f"{rec} überwiegt.",
        "fallback": fallback, "fallback_trigger": "wenn X eintritt",
        "weighted_total": wrong_sum,  # LLM-supplied junk — MUST be ignored
        "conflict_notes": [],
    })


def test_decision_matrix_renders_weighted_sum():
    rendered, valid = validate_template("decision-matrix", _decision_body(), [])
    # A = 2*4 + 3*2 = 14 ; B = 2*1 + 3*5 = 17 — computed by MPR, not the LLM's 999
    assert "**14**" in rendered and "**17**" in rendered
    assert "999" not in rendered
    assert valid is True


def test_fallback_must_differ_from_recommendation():
    # LB-6: a soft quality warning is rendered inline but does NOT flip valid — the matrix parsed +
    # validated, so it's usable; only a hard parse/schema failure degrades.
    rendered, valid = validate_template("decision-matrix", _decision_body(rec="A", fallback="A"), [])
    assert valid is True
    assert "Fallback = recommendation" in rendered  # visible warning, still rendered


def test_blocking_conflict_appears_in_body():
    conflicts = [Conflict(kind="recommendation", topic="top recommendation", severity="blocking",
                          detector="recommendation",
                          sides=[ConflictSide(roles=["A"], stance="top: X"),
                                 ConflictSide(roles=["B"], stance="top: Y")])]
    rendered, _ = validate_template("decision-matrix", _decision_body(), conflicts)
    assert "Conflict zones" in rendered
    assert "blocking" in rendered and "top recommendation" in rendered


def test_unreferenced_blocking_conflict_is_valid_not_degraded():
    # LB-6: a blocking conflict the model didn't list in conflict_notes stays valid — the rendered matrix
    # must NOT be discarded for raw consolidation. LB-7: the internal "not referenced" bookkeeping note is
    # NOT surfaced in the reader-facing body (it's redundant — the conflict is rendered in its own zone).
    conflicts = [Conflict(kind="recommendation", topic="top recommendation", severity="blocking",
                          detector="recommendation",
                          sides=[ConflictSide(roles=["A"], stance="top: X"),
                                 ConflictSide(roles=["B"], stance="top: Y")])]
    rendered, valid = validate_template("decision-matrix", _decision_body(), conflicts)  # conflict_notes=[]
    assert valid is True                                          # NOT degraded
    assert "not referenced in conflict_notes" not in rendered     # LB-7: internal note suppressed
    assert "Conflict zones" in rendered and "top recommendation" in rendered  # conflict still surfaced
    assert "**14**" in rendered and "**17**" in rendered          # the real matrix is there


def test_recommendation_deviation_from_top_score_warns():
    # recommend A (score 14) while B (17) tops → a deviation note must appear.
    rendered, _ = validate_template("decision-matrix", _decision_body(rec="A", fallback="B"), [])
    assert "deviates from the top score" in rendered


def test_fallback_trigger_no_double_conjunction_or_period():
    # #49: the label already prepends "trigger when"; the model restates "When …" and ends with "." →
    # would render "trigger when When … rises.." . Normalize: strip the leading conjunction + single period.
    body = json.loads(_decision_body())
    body["fallback_trigger"] = "When the load rises sharply."
    rendered, _ = validate_template("decision-matrix", json.dumps(body), [])
    assert "trigger when When" not in rendered            # no doubled conjunction
    assert "rises sharply.." not in rendered              # no doubled period
    assert "trigger when the load rises sharply." in rendered


def test_decision_degenerate_matrix_warns_not_evaluable():
    # LOK-13: the LLM emitted cells whose option/criterion strings don't match the declared lists →
    # the matrix maps NOTHING (all "–", score 0) yet is "complete" by count. Must flag honestly
    # ("no scorable cells") instead of presenting an empty matrix with an invented recommendation.
    body = json.dumps({
        "options": ["A", "B"],
        "criteria": [{"name": "Wartbarkeit", "weight": 2}, {"name": "Kosten", "weight": 3}],
        "cells": [  # names DON'T match the lists → map to nothing
            {"option": "Option-A", "criterion": "Wartung", "score": 4},
            {"option": "Option-B", "criterion": "Preis", "score": 3},
        ],
        "recommendation": "A", "recommendation_rationale": "A überwiegt.",
        "fallback": "B", "fallback_trigger": "wenn X", "conflict_notes": [],
    })
    rendered, valid = validate_template("decision-matrix", body, [])
    assert valid is True                          # parses → usable form, soft warning (LB-6)
    assert "no scorable cells" in rendered        # honest degenerate-matrix flag
    assert "**0**" in rendered                    # scores really are 0 (nothing mapped)


def test_evidence_tier_demoted_conservatively():
    body = json.dumps({
        "summary": "Kurzfazit.",
        "findings": [
            {"claim": "Unbelegte Behauptung", "confidence": "high", "support": []},  # → low
            {"claim": "Gut belegt", "confidence": "high", "support": [
                {"role": "A", "provider": "spark", "quote": "Beleg eins"},
                {"role": "B", "provider": "sonnet", "quote": "Beleg zwei"}]},          # stays high
        ],
    })
    rendered, valid = validate_template("evidence-report", body, [])
    low_idx = rendered.index("### Low confidence")
    high_idx = rendered.index("### High confidence")        # the well-supported claim stays high
    high_block = rendered[high_idx:low_idx]                  # sections render high → medium → low
    assert "Gut belegt" in high_block
    assert "Unbelegte Behauptung" not in high_block          # demoted out of high
    assert "Unbelegte Behauptung" in rendered[low_idx:]      # into low


def test_resolve_tier_never_upgrades():
    # a 'low'-labelled but well-supported claim is NOT raised to high (only demotion allowed).
    f = Finding(claim="x", confidence="low", support=[
        {"role": "A", "provider": "p", "quote": "q1"}, {"role": "B", "provider": "p", "quote": "q2"}])
    assert resolve_tier(f, []) == "low"


def test_comparison_has_gaps_opportunities():
    body = json.dumps({
        "options": ["Wir", "Wettbewerber"],
        "criteria": [{"name": "Preis", "weight": 3}, {"name": "UX", "weight": 2}],
        "cells": [
            {"option": "Wir", "criterion": "Preis", "score": 4},
            {"option": "Wir", "criterion": "UX", "score": 3},
            {"option": "Wettbewerber", "criterion": "Preis", "score": 2},
            {"option": "Wettbewerber", "criterion": "UX", "score": 5},
        ],
        "gaps": ["UX hinkt hinterher"], "opportunities": ["Preis-Differenzierung"],
    })
    rendered, valid = validate_template("comparison-matrix", body, [])
    assert "### Gaps" in rendered and "### Opportunities" in rendered
    assert "UX hinkt hinterher" in rendered and valid is True


def test_parse_fail_returns_invalid_with_conflicts_preserved():
    conflicts = [Conflict(kind="claim", topic="sicherheit", severity="material", detector="claim",
                          sides=[ConflictSide(roles=["A"], stance="sicher"),
                                 ConflictSide(roles=["B"], stance="nicht sicher")])]
    rendered, valid = validate_template("decision-matrix", "Das ist gar kein JSON.", conflicts)
    assert valid is False
    assert "sicherheit" in rendered  # conflict value never lost


def test_unknown_template_best_effort():
    rendered, valid = validate_template("no-such-template", "irgendwas", [])
    assert valid is False and "irgendwas" in rendered


# ── risk-register (Spec 05 §5 binding / §7.4 panel) — the formerly-dead risk-assessment domain ───────
def _risk_body(mitig="Backup-Strategie"):
    return json.dumps({
        "summary": "Risikolage zusammengefasst.",
        "risks": [
            {"risk": "Geringes Reputationsrisiko", "severity": "low", "likelihood": "low",
             "mitigation": "Monitoring", "owner": "PR", "roles": ["Reputation"]},
            {"risk": "Single Point of Failure", "severity": "high", "likelihood": "high",
             "mitigation": mitig, "owner": "Eng", "roles": ["Technisch"]},
        ],
        "open_questions": ["Wer trägt das Restrisiko?"],
    })


def test_risk_register_renders_table_worst_first():
    rendered, valid = validate_template("risk-register", _risk_body(), [])
    assert valid is True
    assert "| Risk | Severity | Likelihood | Mitigation | Owner |" in rendered
    # worst exposure (high/high) must lead the low/low risk.
    assert rendered.index("Single Point of Failure") < rendered.index("Geringes Reputationsrisiko")
    assert "high" in rendered and "Wer trägt das Restrisiko?" in rendered


def test_risk_register_warns_on_missing_mitigation():
    # LB-6: soft warning rendered inline but valid stays True (the register parsed + validated).
    rendered, valid = validate_template("risk-register", _risk_body(mitig=""), [])
    assert valid is True and "without mitigation" in rendered


def test_risk_register_keeps_conflict_zones_on_parse_fail():
    conflicts = [Conflict(kind="claim", topic="lieferkette", severity="blocking", detector="claim",
                          sides=[ConflictSide(roles=["Operativ"], stance="stabil"),
                                 ConflictSide(roles=["Finanziell"], stance="fragil")])]
    rendered, valid = validate_template("risk-register", "kein json", conflicts)
    assert valid is False and "lieferkette" in rendered


# ── #44 i18n: English is the source/default; ``de`` is a shipped locale overlay ───────────────────────
def test_render_language_default_is_english():
    # No use_language() call → the renderers must emit the English source headings.
    rendered, _ = validate_template("decision-matrix", _decision_body(), [])
    assert "| Criterion (wt.) |" in rendered and "| **Weighted score** |" in rendered
    assert "**Recommendation:**" in rendered and "**Fallback:**" in rendered


def test_render_language_de_overlay_round_trip():
    # The autouse _reset_render_language fixture restores "en" afterward, so this can't leak.
    from mpr import i18n
    i18n.use_language("de")
    dec, _ = validate_template("decision-matrix", _decision_body(), [])
    assert "| Kriterium (Gew.) |" in dec and "**Empfehlung:**" in dec and "**Rückzugsoption:**" in dec
    risk, _ = validate_template("risk-register", _risk_body(), [])
    assert "| Risiko | Schwere | Eintritt | Mitigation | Owner |" in risk and "hoch" in risk


def test_decision_rationale_prompt_forbids_invented_sums():
    # #48: the decision synthesis instruction must forbid the model from stating its own numeric sums in
    # recommendation_rationale (MPR computes the weighted score) — en source + de overlay both carry it.
    from mpr.templates.prompts import _MODE_EXTRA
    from mpr import i18n
    en = _MODE_EXTRA["decision"]
    assert "recommendation_rationale" in en and "numeric" in en.lower()
    de = i18n.localized(en, "de", "synthesis", "mode_extra", "decision")
    assert de != en and "recommendation_rationale" in de and "erfundenen Zahlen" in de
