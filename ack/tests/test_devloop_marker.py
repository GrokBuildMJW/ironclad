"""Guard-evidence marker + merge-without-evidence reconciler (epic #262, S8 / ADR 0002), offline.

Lives in `scripts/devloop/` (private) -> skips in an installed/clean-room tree. Pins the HMAC marker
(round-trips, rejects a tampered tree / forged marker), the K deploy-seam (inert without the env
key), and the reconciler (a valid merge passes; a markerless OR forged merge is a violation).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_MARKER = _REPO / "scripts" / "devloop" / "marker.py"

pytestmark = pytest.mark.skipif(
    not _MARKER.is_file(),
    reason="private dev-loop marker (scripts/devloop/marker.py) absent — clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_devloop_marker", _MARKER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_SECRET = b"ci-only-secret"
_GATES = {"boundary": 0, "pytest": 0, "doc-reality-audit": 0}


def test_marker_round_trips_and_rejects_tampering():
    m = _load()
    mk = m.compute_marker("tree123", _GATES, _SECRET)
    assert m.verify_marker("tree123", _GATES, _SECRET, mk)
    assert not m.verify_marker("OTHER_TREE", _GATES, _SECRET, mk)          # tree changed
    assert not m.verify_marker("tree123", {"boundary": 1}, _SECRET, mk)    # a gate went red
    assert not m.verify_marker("tree123", _GATES, _SECRET, "deadbeef")     # forged marker


def test_key_seam_inert_without_env(monkeypatch):
    m = _load()
    monkeypatch.delenv(m.KEY_ENV, raising=False)
    assert m.key_from_env() is None and not m.is_active()
    monkeypatch.setenv(m.KEY_ENV, "set")
    assert m.key_from_env() == b"set" and m.is_active()


def test_reconciler_flags_markerless_and_forged():
    m = _load()
    good = {"sha": "aaa1111", "tree_sha": "t1", "gate_results": _GATES,
            "marker": m.compute_marker("t1", _GATES, _SECRET)}
    markerless = {"sha": "bbb2222", "tree_sha": "t2", "gate_results": _GATES}
    forged = {"sha": "ccc3333", "tree_sha": "t3", "gate_results": _GATES, "marker": "deadbeef"}

    assert m.merge_evidence_violations([good], _SECRET) == []              # valid evidence
    v = m.merge_evidence_violations([good, markerless, forged], _SECRET)
    assert any("no guard-evidence marker" in x for x in v)
    assert any("marker mismatch" in x for x in v)
    assert len(v) == 2

    assert m.merge_evidence_violations([markerless, forged], None) == []   # inert without K


def test_read_marker_from_message():
    m = _load()
    mk = "a" * 64
    assert m.read_marker_from_message(f"feat: x\n\n{m.MARKER_TRAILER}: {mk}\n") == mk
    assert m.read_marker_from_message("feat: no trailer here") is None
    # last occurrence wins
    assert m.read_marker_from_message(f"{m.MARKER_TRAILER}: {'b'*64}\n{m.MARKER_TRAILER}: {mk}") == mk


def test_verify_head_commit_inert_grandfathered_malformed_and_forged():
    m = _load()
    good = m.compute_marker("t1", _GATES, _SECRET)
    msg_ok = f"feat: x\n\n{m.MARKER_TRAILER}: {good}"
    assert m.verify_head_commit(msg_ok, secret=None) == []                         # inert without K
    assert m.verify_head_commit("no trailer", secret=_SECRET) == []                # grandfathered
    bad = m.verify_head_commit(f"{m.MARKER_TRAILER}: not-hex", secret=_SECRET)
    assert bad and "malformed" in bad[0]
    forged = m.verify_head_commit(f"{m.MARKER_TRAILER}: {'a'*64}", secret=_SECRET,
                                  tree_sha="t1", gate_results=_GATES)
    assert forged and "marker mismatch" in forged[0]
    assert m.verify_head_commit(msg_ok, secret=_SECRET, tree_sha="t1", gate_results=_GATES) == []  # valid
