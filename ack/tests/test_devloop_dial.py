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


# ── #348 S7: GO binds tree_sha + version; single-use (replay refused) ──
def test_go_binds_tree_sha_and_version():
    d = _load()
    go = d.compute_go(356, "DELIVER", "alice", _SECRET, tree_sha="abc", version="0.0.16")
    assert d.verify_go(go, unit=356, gate="DELIVER", operator="alice", secret=_SECRET, tree_sha="abc", version="0.0.16")
    # a captured GO does NOT authorize a different tree or version than the operator saw
    assert not d.verify_go(go, unit=356, gate="DELIVER", operator="alice", secret=_SECRET, tree_sha="OTHER", version="0.0.16")
    assert not d.verify_go(go, unit=356, gate="DELIVER", operator="alice", secret=_SECRET, tree_sha="abc", version="9")
    # the MERGE gate carries no release artifact (empty tree/version) — still round-trips internally
    m = d.compute_go(274, "MERGE", "alice", _SECRET)
    assert d.verify_go(m, unit=274, gate="MERGE", operator="alice", secret=_SECRET)


def test_go_binds_release_index():
    # #395 S14a (blocker D1-1): test-pypi vs production differ ONLY in release_index, so the GO MUST bind it —
    # else a Test-PyPI GO is byte-identical to a production GO at the same HEAD and "Test-PyPI FIRST" is
    # operator-discipline-only.
    d = _load()
    testpypi = d.compute_go(364, "DELIVER", "alice", _SECRET, tree_sha="abc", version="0.0.16", release_index="testpypi")
    assert d.verify_go(testpypi, unit=364, gate="DELIVER", operator="alice", secret=_SECRET,
                       tree_sha="abc", version="0.0.16", release_index="testpypi")
    # a Test-PyPI GO is REJECTED for the production index
    assert not d.verify_go(testpypi, unit=364, gate="DELIVER", operator="alice", secret=_SECRET,
                           tree_sha="abc", version="0.0.16", release_index="pypi")
    # the two indices yield DIFFERENT tokens at the same HEAD/version
    pypi = d.compute_go(364, "DELIVER", "alice", _SECRET, tree_sha="abc", version="0.0.16", release_index="pypi")
    assert testpypi != pypi
    # authorize_advance refuses the wrong-index GO too (the consume path)
    no, why = d.authorize_advance("DELIVER", d.FROZEN_DIAL, go=testpypi, unit=364, operator="alice",
                                  secret=_SECRET, tree_sha="abc", version="0.0.16", release_index="pypi")
    assert not no and "index" in why


def test_authorize_advance_refuses_a_replayed_go():
    d = _load()
    go = d.compute_go(356, "DELIVER", "alice", _SECRET, tree_sha="abc", version="1")
    spent = [{"payload": {"go_consumed": go}}]                  # a prior consumed record in the ledger
    ok, why = d.authorize_advance("DELIVER", d.FROZEN_DIAL, go=go, unit=356, operator="alice",
                                  secret=_SECRET, tree_sha="abc", version="1", ledger_records=spent)
    assert not ok and "consumed" in why
    # not-yet-consumed -> authorized
    ok2, _ = d.authorize_advance("DELIVER", d.FROZEN_DIAL, go=go, unit=356, operator="alice",
                                 secret=_SECRET, tree_sha="abc", version="1", ledger_records=[])
    assert ok2


def test_go_secret_resolves_env_then_file(monkeypatch, tmp_path):
    # #348 S7: provision once via env OR a file (default ~/.devloop/go_secret, outside the repo) so it is
    # always available without re-exporting; env wins, file is the fallback, neither -> inert.
    d = _load()
    monkeypatch.delenv("GX10_DEVLOOP_GO_SECRET", raising=False)
    f = tmp_path / "go_secret"
    monkeypatch.setenv("GX10_DEVLOOP_GO_SECRET_FILE", str(f))
    assert d.go_secret_from_env() is None                       # no env, no file -> inert
    f.write_text("  file-secret-123  ", encoding="utf-8")
    assert d.go_secret_from_env() == b"file-secret-123"         # file fallback (whitespace stripped)
    monkeypatch.setenv("GX10_DEVLOOP_GO_SECRET", "env-secret")
    assert d.go_secret_from_env() == b"env-secret"              # env wins over the file
