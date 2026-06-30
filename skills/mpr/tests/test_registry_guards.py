"""Panel guards (skills/mpr/registry/guards.py) — Distinctness & Coverage (Spec 05 §7.6 / §10).

Deterministic, no LLM: rephrasings are flagged while the real start-panels pass distinctness, a
missing axis is flagged while the start-panels cover their COVERAGE_AXES, adhoc/unknown domains are a
coverage no-op, and required_axes can be overridden. The shared jaccard/lens_signature helpers behave.
"""
from __future__ import annotations

from pathlib import Path

from mpr.registry.guards import (
    COVERAGE_AXES,
    DISTINCTNESS_MAX_OVERLAP,
    check_coverage,
    check_distinctness,
    jaccard,
    lens_signature,
)
from mpr.registry.loader import PanelRegistry
from mpr.registry.schema import Panel, Role

_MPR_ROOT = Path(__file__).resolve().parents[1]
_EXPECTED = {"architecture-decision", "regulatory", "competitive", "risk-assessment"}


def _registry() -> PanelRegistry:
    reg = PanelRegistry()
    reg.discover(_MPR_ROOT)
    return reg


def _panel(domain: str, roles: list[dict], evidence="internal", mode="decision",
           template="decision-matrix") -> Panel:
    return Panel.model_validate({
        "domain": domain, "mode": mode, "evidence_source": evidence,
        "synthesis_template": template, "effort_defaults": {"default": "medium"}, "roles": roles,
    })


# ── shared helpers ───────────────────────────────────────────────────────────────────────────────
def test_jaccard_basic():
    assert jaccard(set(), set()) == 0.0
    assert jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert jaccard({"a", "b"}, {"a", "c"}) == 1 / 3


def test_lens_signature_drops_stopwords():
    r = Role(role="X", lens_prompt="Du bist der Maintainer und bewertest die Wartbarkeit")
    sig = lens_signature(r)
    assert "der" not in sig and "die" not in sig and "und" not in sig
    assert any(t.startswith("maintain") for t in sig)


def test_stem_longest_suffix_wins():
    # #503 MPR-REG-4: the LONGEST matching suffix wins, so "Wartbarkeit" trims "barkeit" → "wart" and
    # clusters with "Wartung"/"wartbar". A first-match scan stripped the shorter "keit" first
    # ("wartbarkeit" → "wartbar"), leaving "barkeit" unreachable and the forms in separate clusters.
    from mpr.registry.guards import _stem
    assert _stem("wartbarkeit") == "wart"
    assert _stem("wartung") == "wart"
    assert _stem("wartbar") == "wart"
    assert len({_stem("wartbarkeit"), _stem("wartung"), _stem("wartbar")}) == 1


# ── distinctness ─────────────────────────────────────────────────────────────────────────────────
def test_distinctness_flags_rephrasings():
    panel = _panel("competitive", [
        {"role": "A", "lens_prompt": "Vergleiche Preismodelle und Packaging der Wettbewerber genau"},
        {"role": "B", "lens_prompt": "Vergleiche Preismodelle und Packaging der Wettbewerber genau"},
        {"role": "C", "lens_prompt": "Bewerte technologische Burggräben und Netzwerk-Effekte klar"},
    ], evidence="external", mode="comparison", template="comparison-matrix")
    findings = check_distinctness(panel)
    assert findings
    assert any("rephrasings" in f and "'A'" in f and "'B'" in f for f in findings)


def test_distinctness_passes_for_start_panels():
    reg = _registry()
    for dom in _EXPECTED:
        assert check_distinctness(reg.resolve(dom)) == [], f"{dom} has rephrasing roles"


def test_distinctness_threshold_is_point_seven():
    assert DISTINCTNESS_MAX_OVERLAP == 0.7


# ── coverage ─────────────────────────────────────────────────────────────────────────────────────
def test_coverage_flags_missing_axis():
    # an architecture-decision panel covering only security/performance/cost → the rest are flagged.
    panel = _panel("architecture-decision", [
        {"role": "Sec", "lens_prompt": "Security Zero-Trust Angriffsfläche bewerten"},
        {"role": "Perf", "lens_prompt": "Performance Durchsatz Latenz unter Last"},
        {"role": "Cost", "lens_prompt": "Kosten TCO Lizenz über den Lebenszyklus"},
    ])
    findings = check_coverage(panel)
    joined = " ".join(findings)
    assert "maintainability" in joined and "operability" in joined and "team-fit" in joined
    assert "security" not in joined and "performance" not in joined and "cost" not in joined


def test_coverage_passes_for_start_panels():
    reg = _registry()
    for dom in _EXPECTED:
        assert check_coverage(reg.resolve(dom)) == [], f"{dom} misses a COVERAGE axis"


def test_coverage_adhoc_is_noop():
    panel = _panel("adhoc", [
        {"role": "A", "lens_prompt": "irgendeine Brille eins"},
        {"role": "B", "lens_prompt": "andere Brille zwei"},
        {"role": "C", "lens_prompt": "dritte Brille drei"},
    ])
    assert check_coverage(panel) == []  # no reference axes for adhoc


def test_coverage_required_axes_override():
    panel = _panel("architecture-decision", [
        {"role": "Sec", "lens_prompt": "Security Zero-Trust"},
        {"role": "B", "lens_prompt": "etwas anderes"},
        {"role": "C", "lens_prompt": "noch etwas"},
    ])
    findings = check_coverage(panel, required_axes=["security", "no-such-axis"])
    assert findings == ["axis 'no-such-axis' uncovered — add a role"]


def test_coverage_axes_table_matches_spec():
    assert COVERAGE_AXES["architecture-decision"] == [
        "maintainability", "operability", "security", "performance",
        "reversibility", "team-fit", "cost",
    ]
    assert COVERAGE_AXES["risk-assessment"] == [
        "technical", "operational", "regulatory", "financial", "reputation",
    ]


# ── regression: start-panels pass ALL guards ────────────────────────────────────────────────────
def test_start_panels_pass_all_guards():
    reg = _registry()
    for dom in _EXPECTED:
        panel = reg.resolve(dom)
        assert check_distinctness(panel) == []
        assert check_coverage(panel) == []
