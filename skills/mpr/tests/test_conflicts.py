"""Cross-verify conflict detector (skills/mpr/conflicts.py) — Spec 06 §3 / §8-B. LLM-free, isolated.

Each sub-detector fires on engineered inputs; agreement yields no false positive; conflicts merge by
(kind, topic) with severity=max and sort blocking-first; a throwing sub-detector is fail-soft.
"""
from __future__ import annotations

from types import SimpleNamespace

from mpr.conflicts import (
    Conflict,
    ConflictSide,
    _merge_and_sort,
    detect_conflicts,
)


def P(role, content):
    return SimpleNamespace(role=role, content=content)


def test_detect_recommendation_conflict_blocking():
    ok = [P("Architekt", "Ich empfehle Postgres für diesen Workload."),
          P("Analyst", "Ich empfehle MongoDB hier klar.")]
    out = detect_conflicts(ok, subjects=["Postgres", "MongoDB"], mode="decision")
    rec = [c for c in out if c.kind == "recommendation"]
    assert rec and rec[0].severity == "blocking"
    assert len(rec[0].sides) == 2


def test_detect_numeric_conflict_by_spread():
    ok = [P("A", "Der Durchsatz liegt bei 120 tok/s."), P("B", "Wir messen nur 20 tok/s.")]
    out = detect_conflicts(ok, subjects=[])
    num = [c for c in out if c.kind == "numeric"]
    assert num and num[0].topic == "tok/s"
    assert num[0].severity == "blocking"  # (120-20)/median(70)=1.43 > 1.0


def test_detect_polarity_pro_contra():
    ok = [P("A", "Ich empfehle Postgres klar."),
          P("B", "Postgres sollte man unbedingt vermeiden.")]
    out = detect_conflicts(ok, subjects=["Postgres"], mode="decision")
    pol = [c for c in out if c.kind == "polarity"]
    assert pol and pol[0].topic == "Postgres"
    stances = {s.stance for s in pol[0].sides}
    assert "pro Postgres" in stances and "contra Postgres" in stances


def test_infer_subjects_drops_stopwords():
    # LB-7: German capitalises prepositions/articles at sentence start ("Für", "Die") → without filtering
    # they became bogus subjects ("Für k"). They must be dropped even when present in the query.
    ok = [P("A", "Für die Wahl empfehle ich Modulith. Die Lösung ist gut."),
          P("B", "Für die Wahl sollte man Modulith vermeiden. Die Lösung ist schlecht.")]
    q = "Für die Architektur: Modulith oder Microservices?"
    pol = {c.topic for c in detect_conflicts(ok, subjects=None, mode="decision", query=q)
           if c.kind == "polarity"}
    assert "Modulith" in pol
    assert "Für" not in pol and "Die" not in pol     # stopwords never become subjects


def test_claim_stance_cleaned_markdown_and_version_intact():
    # LOK-12: a claim stance is a RAW model sentence — list/inline markdown must be stripped and a
    # version number ("Qwen3.6") must not split into a dangling "6 35B A3B" fragment.
    ok = [
        P("Team-Fit", "- **Warum** das Qwen3.6 35B A3B Modell nicht performant genug ist."),
        P("SRE", "Das Qwen3.6 35B A3B Modell ist performant genug."),
    ]
    claims = [c for c in detect_conflicts(ok, subjects=[]) if c.kind == "claim"]
    assert claims, "expected a claim conflict"
    stances = [s.stance for c in claims for s in c.sides]
    assert all("**" not in s for s in stances)            # inline markdown stripped
    assert all(not s.startswith("- ") for s in stances)   # list prefix stripped
    assert any("Qwen3.6 35B A3B" in s for s in stances)   # version intact (sentence not split at 3.6)
    assert all(s != "6 35B A3B" for s in stances)         # no version-split fragment


def test_numeric_ignores_ambiguous_units():
    # LB-7: bare "x" (multiplier) / "k" (scale) are too ambiguous to anchor a conflict → no numeric
    # conflict; concrete units (ms) still fire.
    ok = [P("A", "Bun ist 3x schneller und schafft 50k Anfragen."),
          P("B", "Node ist nur 1x so schnell, etwa 5k Anfragen.")]
    out = detect_conflicts(ok, subjects=[])
    assert [c for c in out if c.kind == "numeric"] == []      # "x"/"k" no longer produce noise
    ok2 = [P("A", "Latenz 5ms."), P("B", "Latenz 200ms.")]
    assert [c for c in detect_conflicts(ok2, subjects=[]) if c.kind == "numeric"]  # ms still fires


def test_infer_subjects_query_anchored():
    # German capitalises every noun → naive inference invents "pro Kosten ↔ contra Kosten" garbage.
    # Anchoring to the query keeps only the real option named in the question (Modulith), drops the
    # generic noun (Kosten) — Live-Bug #4. Without the query anchor the noise returns (documents it).
    ok = [P("A", "Ich empfehle Modulith klar. Ich empfehle Kosten zu senken."),
          P("B", "Modulith sollte man vermeiden. Kosten sollte man vermeiden.")]
    q = "Sollten wir Modulith oder Microservices nehmen?"
    anchored = {c.topic for c in detect_conflicts(ok, subjects=None, mode="decision", query=q)
                if c.kind == "polarity"}
    assert "Modulith" in anchored and "Kosten" not in anchored
    naive = {c.topic for c in detect_conflicts(ok, subjects=None, mode="decision")
             if c.kind == "polarity"}
    assert "Modulith" in naive and "Kosten" in naive          # no anchor → the noise comes back


def test_detect_claim_negation_overlap():
    ok = [P("A", "Das System ist sicher genug."), P("B", "Das System ist nicht sicher genug.")]
    out = detect_conflicts(ok, subjects=[])
    claim = [c for c in out if c.kind == "claim"]
    assert claim and claim[0].severity == "material"


def test_no_false_positive_on_agreement():
    ok = [P("A", "Postgres ist eine gute Wahl für Transaktionen."),
          P("B", "Postgres passt gut für transaktionale Lasten.")]
    assert detect_conflicts(ok, subjects=["Postgres", "MongoDB"], mode="decision") == []


def test_conflicts_merge_and_sort():
    a = Conflict(kind="polarity", topic="X", severity="minor", detector="d",
                 sides=[ConflictSide(roles=["A"], stance="pro X")])
    b = Conflict(kind="polarity", topic="X", severity="blocking", detector="d",
                 sides=[ConflictSide(roles=["B"], stance="contra X")])
    c = Conflict(kind="numeric", topic="ms", severity="material", detector="d",
                 sides=[ConflictSide(roles=["C"], stance="5ms")])
    out = _merge_and_sort([a, b, c])
    assert len(out) == 2                                   # a + b merged on (polarity, X)
    pol = [x for x in out if x.kind == "polarity"][0]
    assert pol.severity == "blocking" and len(pol.sides) == 2  # severity = max, sides merged
    assert out[0].severity == "blocking"                  # blocking sorted first


def test_polarity_negation_is_subject_scoped():
    # HIGH-1: a negation elsewhere in the clause must not flip the subject's stance to contra.
    ok = [P("A", "Ich empfehle Redis, weil es nicht langsam ist."),
          P("B", "Redis sollte man vermeiden.")]
    out = detect_conflicts(ok, subjects=["Redis"], mode="decision")
    pol = [c for c in out if c.kind == "polarity"]
    assert pol, "A=pro (distant negation), B=contra → a polarity conflict must form"
    pro_roles = next(s.roles for s in pol[0].sides if s.stance == "pro Redis")
    assert "A" in pro_roles  # A stayed pro despite the 'nicht langsam' clause


def test_numeric_per_subject_no_false_positive():
    # HIGH-2: same unit, DIFFERENT subjects, each agreeing → no numeric conflict.
    ok = [P("A", "Latenz liegt bei 5ms, Timeout bei 100ms."),
          P("B", "Latenz ist 6ms, Timeout 95ms.")]
    out = detect_conflicts(ok, subjects=["Latenz", "Timeout"])
    assert not [c for c in out if c.kind == "numeric"]


def test_claim_peripheral_negation_no_false_positive():
    # HIGH-3: the negation hits a non-shared token ('kritisch') → not a contradiction of the shared claim.
    ok = [P("A", "Die Latenz ist hoch unter Last."),
          P("B", "Die Latenz ist hoch aber nicht kritisch.")]
    assert not [c for c in detect_conflicts(ok, subjects=[]) if c.kind == "claim"]


def test_recommendation_word_boundary_distinguishes_substring_options():
    # MED-1: 'Pro' must not match inside 'Production'.
    ok = [P("A", "Ich empfehle Pro klar."), P("B", "Ich empfehle Production hier.")]
    out = detect_conflicts(ok, subjects=["Pro", "Production"], mode="decision")
    rec = [c for c in out if c.kind == "recommendation"]
    assert rec and len(rec[0].sides) == 2
    stances = {s.stance for s in rec[0].sides}
    assert "top: Pro" in stances and "top: Production" in stances


def test_polarity_inferred_subject_is_minor():
    # MED-4: a conflict on an INFERRED (not provided) subject is 'minor' (Neben-Subjekt, §3.2).
    ok = [P("A", "Ich empfehle Kubernetes für den Betrieb."),
          P("B", "Kubernetes sollte man hier vermeiden.")]
    out = detect_conflicts(ok, subjects=None)  # inferred
    pol = [c for c in out if c.kind == "polarity" and c.topic == "Kubernetes"]
    assert pol and pol[0].severity == "minor"


def test_detector_fail_soft(monkeypatch):
    import mpr.conflicts as C

    def _boom(*a, **k):
        raise RuntimeError("detector blew up")

    monkeypatch.setattr(C, "_numeric", _boom)  # one sub-detector throws
    ok = [P("A", "Ich empfehle Postgres."), P("B", "Ich empfehle MongoDB.")]
    out = C.detect_conflicts(ok, subjects=["Postgres", "MongoDB"], mode="decision")
    assert any(c.kind == "recommendation" for c in out)  # others still returned, no exception
