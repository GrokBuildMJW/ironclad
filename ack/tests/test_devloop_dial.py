"""Supervised-gate authorisation + dial (epic #262, S12 / ADR 0002), offline.

Lives in `scripts/devloop/` (private) -> skips in an installed/clean-room tree. Pins the GO token
(operator-identity-bound, rejects a wrong gate/operator/forged token), the dial disposition (human
gates supervised by default; auto overridable), and authorisation (auto advances; supervised needs
a valid GO, else the unit is parked; a forged/absent GO is refused).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_DIAL = _REPO / "scripts" / "devloop" / "dial.py"

pytestmark = pytest.mark.skipif(
    not _DIAL.is_file(),
    reason="private dev-loop dial (scripts/devloop/dial.py) absent — clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_devloop_dial", _DIAL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_SECRET = b"go-secret"


def test_go_token_is_identity_bound():
    d = _load()
    tok = d.compute_go(274, "MERGE", "alice", _SECRET)
    assert d.verify_go(tok, unit=274, gate="MERGE", operator="alice", secret=_SECRET)
    assert not d.verify_go(tok, unit=274, gate="DELIVER", operator="alice", secret=_SECRET)  # wrong gate
    assert not d.verify_go(tok, unit=274, gate="MERGE", operator="mallory", secret=_SECRET)  # wrong operator
    assert not d.verify_go("deadbeef", unit=274, gate="MERGE", operator="alice", secret=_SECRET)  # forged


def test_dial_disposition_defaults_and_override():
    d = _load()
    assert d.gate_disposition("MERGE", {}) == "supervised"      # human gate default
    assert d.gate_disposition("GATE", {}) == "auto"             # non-human default
    assert d.gate_disposition("MERGE", {"MERGE": "auto"}) == "auto"   # phase-2 relax by config
    assert d.FROZEN_DIAL["MERGE"] == "supervised" and d.FROZEN_DIAL["DELIVER"] == "supervised"


def test_authorize_advance_supervised_needs_valid_go():
    d = _load()
    # auto gate advances with nothing
    ok, _ = d.authorize_advance("GATE", {})
    assert ok
    # supervised, no GO => parked (blocked, not failed)
    parked, why = d.authorize_advance("MERGE", d.FROZEN_DIAL)
    assert not parked and "parked" in why
    # supervised, valid GO => advance
    tok = d.compute_go(274, "MERGE", "alice", _SECRET)
    yes, _ = d.authorize_advance("MERGE", d.FROZEN_DIAL, go=tok, unit=274, operator="alice", secret=_SECRET)
    assert yes
    # supervised, forged GO => refused
    no, why2 = d.authorize_advance("MERGE", d.FROZEN_DIAL, go="bad", unit=274, operator="alice", secret=_SECRET)
    assert not no and "forged" in why2
