"""Machine-gated dev-loop unit SELECT (epic #312 S3 / the GitHub-I/O seam), offline.

Lives in `scripts/devloop/` (private) -> skips in an installed/clean-room tree. Pins the PURE selection
policy: a blocking/in-review/needs-decision status and the skip-set are excluded (source/upstream|plugin
mirrors are re-admitted, #361 S11); ordering is priority-then-number; a malformed unit is rejected with a
named reason. Also pins the blocking-label SSOT shared with coupling.

The module is ``selection.py`` (NOT ``select.py``) deliberately: a sibling named ``select`` would shadow
the stdlib ``select`` on ``sys.path[0]`` when a devloop script runs, crashing the stdlib import chain
``subprocess→selectors→select`` — see #337 + the ``devloop-no-stdlib-shadow`` invariant.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SELECT = _REPO / "scripts" / "devloop" / "selection.py"

pytestmark = pytest.mark.skipif(
    not _SELECT.is_file(),
    reason="private dev-loop selection (scripts/devloop/selection.py) absent — clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_devloop_selection", _SELECT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _c(number, *labels):
    return {"number": number, "labels": list(labels)}


def test_eligibility_excludes_blocked_review_and_skip_but_admits_source():
    s = _load()
    cands = [
        _c(10, "type/task"),                                  # eligible
        _c(11, "status/blocked"),                             # blocked -> out
        _c(12, "status/in-review"),                           # in-review -> out (#278 spirit)
        _c(13, "status/needs-decision"),                      # unresolved C0 fork -> out
        _c(14, "source/upstream", "type/bug"),               # upstream mirror -> ADMITTED (#361 S11: round-trip verified)
        _c(15, "source/plugin"),                              # plugin mirror -> ADMITTED
        _c(16, "type/task"),                                  # eligible but in skip-set
    ]
    elig = s.eligible_units(cands, skip=[16])
    assert [c["number"] for c in elig] == [10, 14, 15]       # source mirrors re-admitted; blocked/review/skip out


def test_ordering_is_priority_then_number():
    s = _load()
    cands = [
        _c(30, "type/task"),                                  # medium (default)
        _c(31, "priority/low"),
        _c(20, "priority/high"),
        _c(25, "priority/high"),                              # high, lower number first
    ]
    assert [c["number"] for c in s.eligible_units(cands)] == [20, 25, 30, 31]
    assert s.next_unit(cands)["number"] == 20                 # the single next unit
    assert s.next_unit([_c(1, "status/blocked")]) is None     # no eligible unit -> halt


def test_malformed_unit_is_rejected_with_a_reason():
    s = _load()
    assert s.malformed_reasons({"number": 0, "parent_epic": 312})                # no valid number
    assert any("parent epic" in r for r in s.malformed_reasons({"number": 7}))   # no linked epic
    assert s.malformed_reasons({"number": 7, "parent_epic": 312}) == []          # well-formed


def _job(name, conclusion, status="completed"):
    return {"name": name, "status": status, "conclusion": conclusion}


def test_ci_verdict_green_pending_and_no_ci_is_not_green():
    s = _load()
    assert s.ci_verdict([_job("tests", "success"), _job("boundary", "skipped")])["verdict"] == "green"
    assert s.ci_verdict([_job("tests", None, status="in_progress")])["verdict"] == "pending"
    nc = s.ci_verdict([])
    assert nc["verdict"] == "no-ci" and "NOT green" in nc["reasons"][0]           # no CI fired != green


def test_ci_verdict_failure_is_red_on_first_but_transient_retries():
    s = _load()
    # a real failure is a defect -> RED on the first observation, no retry
    assert s.ci_verdict([_job("tests", "failure")], attempt=0)["verdict"] == "red"
    # a transient-infra conclusion is retry-eligible within the cap...
    assert s.ci_verdict([_job("tests", "timed_out")], attempt=0, cap=2)["verdict"] == "retry"
    # ...but RED once the retry budget is exhausted
    assert s.ci_verdict([_job("tests", "timed_out")], attempt=2, cap=2)["verdict"] == "red"
    # a mix of transient + a hard failure is RED (the hard one dominates)
    assert s.ci_verdict([_job("a", "timed_out"), _job("b", "failure")], attempt=0)["verdict"] == "red"


def _load_sibling(stem):
    import importlib.util
    p = _SELECT.parent / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(f"_devloop_{stem}", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_blocking_label_ssot_is_drift_proof():
    # #361 S11: selection + coupling derive their blocking sets from ONE spec SSOT, so a label rename
    # cannot drift them. selection skips the broader set (C0 forks + in-review); coupling.c0_present
    # blocks only the C0 forks. The only difference is status/in-review, by construction.
    s = _load()
    spec = _load_sibling("spec")
    coupling = _load_sibling("coupling")
    # value equality (the modules are loaded independently per test, so identity is a load artifact; in
    # production spec is imported once and both reference the same object).
    assert s._BLOCKING == spec.BLOCKING_STATUS_LABELS
    assert coupling._BLOCKING_LABELS == spec.C0_FORK_LABELS
    assert spec.C0_FORK_LABELS <= spec.BLOCKING_STATUS_LABELS                       # the base is contained
    assert s._BLOCKING - coupling._BLOCKING_LABELS == {"status/in-review"}          # the only documented delta
