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


# ── #348 S9: merge-walk / delivery_record / HWM (the delivery-surface DATA) ──
def test_merge_walk_assembles_delivery_merges_and_excludes_non_delivery():
    m = _load()
    records = [
        {"seq": 0, "payload": {"surface": "DELIVER", "go_consumed": "x", "unit": 1}},   # a consume, not delivered
        {"seq": 1, "payload": {"surface": "DELIVER", "status": "delivered", "sha": "s1",
                                "tree_sha": "t1", "gate_results": {"clean-room": 0}, "marker": "abc"}},
        {"seq": 2, "payload": {"src": "CI", "dst": "MERGE", "guard": "merge-go"}},        # a human per-unit merge
    ]
    merges = m.merge_walk(records)
    assert len(merges) == 1 and merges[0]["sha"] == "s1" and merges[0]["marker"] == "abc"
    assert m.merge_walk(records, high_water_mark=2) == []                                 # delivery below HWM grandfathered


def test_a_human_per_unit_merge_is_never_a_violation():
    m = _load()
    human = [{"seq": 0, "payload": {"src": "CI", "dst": "MERGE", "guard": "merge-go"}}]
    assert m.merge_walk(human) == [] and m.merge_evidence_violations(m.merge_walk(human), b"K") == []


def test_delivery_record_is_pending_at_push_inert_without_k_valid_with_k():
    # #396 S14b: the push-time stamp is DELIVERED-PENDING (NOT terminal) — done-means-deployed. The terminal
    # `delivered` (which merge_walk/published key on) is written only by the completion gate.
    m = _load()
    inert = m.delivery_record(sha="s", tree_sha="t", gate_results={"g": 0}, secret=None, unit=358)
    assert inert["marker"] is None and inert["surface"] == "DELIVER" and inert["status"] == "delivered-pending"
    live = m.delivery_record(sha="s", tree_sha="t", gate_results={"g": 0}, secret=b"K", unit=358)
    assert live["marker"] and m.verify_marker("t", {"g": 0}, b"K", live["marker"])         # forge-resistant
    # the completion gate writes the terminal status; the marker rides the same record
    terminal = m.delivery_record(sha="s", tree_sha="t", gate_results={"g": 0}, secret=b"K", unit=358, status="delivered")
    assert terminal["status"] == "delivered" and terminal["marker"] == live["marker"]
    # #397 S14c: the record carries version + release_index (the Test-PyPI-first guard keys on them)
    idx = m.delivery_record(sha="s", tree_sha="t", gate_results={"g": 0}, secret=b"K", unit=358,
                            status="delivered", version="0.0.16", release_index="testpypi")
    assert idx["version"] == "0.0.16" and idx["release_index"] == "testpypi"


def test_reconciler_on_walked_merges_flags_markerless_with_k():
    m = _load()
    # the merge-walk verifies TERMINAL delivered records (the completion-gate output), so build those
    good = m.delivery_record(sha="s1", tree_sha="t1", gate_results={"g": 0}, secret=b"K", unit=1, status="delivered")
    bad = m.delivery_record(sha="s2", tree_sha="t2", gate_results={"g": 0}, secret=None, unit=2, status="delivered")  # no marker
    merges = m.merge_walk([{"seq": 0, "payload": good}, {"seq": 1, "payload": bad}])
    v = m.merge_evidence_violations(merges, b"K")
    assert any("s2" in x for x in v) and not any("s1" in x for x in v)


def test_high_water_mark_round_trips(tmp_path):
    m = _load()
    f = tmp_path / "hwm"
    assert m.read_high_water_mark(f) == 0
    m.write_high_water_mark(7, f)
    assert m.read_high_water_mark(f) == 7


def test_merge_walk_built_is_true_since_s9():
    assert _load().MERGE_WALK_BUILT is True


def test_activation_preflight_refuses_unbuilt_walk_and_double_activation():
    m = _load()
    # happy: walk built, K not yet set -> ok, HWM = ledger length (grandfather everything before now)
    ok, reasons, hwm = m.activation_preflight(key_set=False, walk_built=True, ledger_len=12)
    assert ok and reasons == [] and hwm == 12
    # interlock: a premature K (walk unbuilt) is refused, HWM 0
    ok, reasons, hwm = m.activation_preflight(key_set=False, walk_built=False, ledger_len=12)
    assert not ok and any("UNBUILT" in r for r in reasons) and hwm == 0
    # idempotency: K already set is refused (re-activation would re-grandfather the window)
    ok, reasons, _ = m.activation_preflight(key_set=True, walk_built=True, ledger_len=12)
    assert not ok and any("already set" in r for r in reasons)
