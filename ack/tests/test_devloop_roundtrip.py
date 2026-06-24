"""Engine-verified upstream round-trip close-out (#348 S11), offline.

Lives in `scripts/devloop/` (private) -> skips in an installed/clean-room tree. Pins the parser, the
FAIL-CLOSED close-out verification (unreadable public side / mid-flight ref => not done), the named
public-READ seam, and the delivery done-contingency.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_RT = _REPO / "scripts" / "devloop" / "roundtrip.py"

pytestmark = pytest.mark.skipif(
    not _RT.is_file(),
    reason="private dev-loop roundtrip (scripts/devloop/roundtrip.py) absent — clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_devloop_roundtrip", _RT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _issue(number, *labels, state="OPEN"):
    return {"number": number, "labels": list(labels), "state": state}


def test_parse_resolves_upstream_dedupes_and_is_case_insensitive():
    r = _load()
    body = "Closes #42\nResolves upstream: ironclad#7\nresolves UPSTREAM: GrokBuildMJW/ironclad#7\nResolves upstream: ironclad#9"
    assert r.parse_resolves_upstream(body) == [7, 9]
    assert r.parse_resolves_upstream("no markers here") == []


def test_issue_closed_out_requires_released_and_closed():
    r = _load()
    assert r.issue_closed_out(_issue(7, "released", state="CLOSED"))
    assert not r.issue_closed_out(_issue(7, "resolved", state="CLOSED"))     # resolved alone = mid-flight
    assert not r.issue_closed_out(_issue(7, "released", state="OPEN"))       # released but still open


def test_close_out_verified_is_fail_closed_on_unreadable_and_midflight():
    r = _load()
    # no refs -> trivially complete
    assert r.close_out_verified([], {}, readable=True) == (True, [])
    # unreadable public side -> NOT ok (fail-closed), even with a would-be-complete cache
    ok, reasons = r.close_out_verified([7], {7: _issue(7, "released", state="CLOSED")}, readable=False)
    assert not ok and any("UNVERIFIABLE" in x for x in reasons)
    # a mid-flight ref (resolved, not released+closed) -> NOT ok
    ok, reasons = r.close_out_verified([7], {7: _issue(7, "resolved", state="OPEN")}, readable=True)
    assert not ok and any("pending" in x for x in reasons)
    # a ref not visible yet -> NOT ok
    ok, reasons = r.close_out_verified([9], {}, readable=True)
    assert not ok and any("not visible" in x for x in reasons)
    # all refs released+closed -> ok
    assert r.close_out_verified([7], {7: _issue(7, "released", state="CLOSED")}, readable=True) == (True, [])


def test_read_public_issues_is_fail_closed_on_a_read_error():
    r = _load()
    def reader(n):
        if n == 9:
            raise RuntimeError("public read failed")
        return _issue(n, "released", state="CLOSED")
    readable, issues = r.read_public_issues([7, 9], reader)
    assert readable is False and 7 in issues and 9 not in issues       # a partial read is fail-closed


def test_delivery_done_contingency():
    r = _load()
    closed = lambda n: _issue(n, "released", state="CLOSED")
    midflight = lambda n: _issue(n, "resolved", state="OPEN")
    # shipped + every ref closed-out -> done
    done, _ = r.delivery_done(delivered=True, body="Resolves upstream: ironclad#7", reader=closed)
    assert done
    # shipped but a ref mid-flight -> NOT done (DELIVERED-PENDING)
    done, reasons = r.delivery_done(delivered=True, body="Resolves upstream: ironclad#7", reader=midflight)
    assert not done and reasons
    # did not ship -> never done
    assert r.delivery_done(delivered=False, body="Resolves upstream: ironclad#7", reader=closed)[0] is False
    # shipped, no upstream refs -> trivially done
    assert r.delivery_done(delivered=True, body="Closes #5", reader=closed)[0] is True
