"""Machine-gated dev-loop spec consistency (epic #262, ADR 0002), offline.

The spec is pure data + a `validate()` self-gate. It lives in `scripts/devloop/` (private) ->
skips in an installed/clean-room tree. Pins that the real spec is internally consistent (the
positive case) AND that `validate()` actually catches an undefined guard reference / an unmapped
rule (the negative case, ADR-0007 discipline) — so the spec cannot silently drift incomplete.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SPEC = _REPO / "scripts" / "devloop" / "spec.py"

pytestmark = pytest.mark.skipif(
    not _SPEC.is_file(),
    reason="private dev-loop spec (scripts/devloop/spec.py) absent — installed/clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_devloop_spec", _SPEC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_real_spec_is_internally_consistent():
    mod = _load()
    assert mod.validate() == []                      # the spec's own first gate is green


def test_spec_shape_is_well_formed():
    mod = _load()
    assert {"SELECT", "GATE", "MERGE", "ABORT", "DELIVER"} <= set(mod.STATES)
    assert set(mod.TARGETS) == {"internal-plugin", "core-monorepo", "test-pypi"}   # +test-pypi (#348 S6)
    # every transition guard resolves to a defined guard
    assert all(g in mod.GUARDS for (_, _, g, _) in mod.TRANSITIONS)
    # rule ids are the contiguous DEV_LOOP set, each mapped
    assert [r[0] for r in mod.RULES] == list(range(1, len(mod.RULES) + 1))


def test_validate_catches_an_undefined_guard_reference():
    mod = _load()
    saved = list(mod.TRANSITIONS)
    try:
        mod.TRANSITIONS.append(("GATE", "PR", "no-such-guard", "in-flight"))
        errs = mod.validate()
        assert any("undefined guard" in e for e in errs)
    finally:
        mod.TRANSITIONS[:] = saved
    assert mod.validate() == []                       # restored


def test_validate_catches_a_phase2b_delivery_gate_leaking_into_a_profile():
    # #312 S6: clean-room/release-preflight/export-sync are DoD (delivery), never a per-unit gate.
    mod = _load()
    gp = mod.TARGETS["core-monorepo"]["gate_profile"]
    saved = list(gp)
    try:
        gp.append("clean-room")
        errs = mod.validate()
        assert any("Phase-2b delivery gate" in e and "clean-room" in e for e in errs)
    finally:
        gp[:] = saved
    assert mod.validate() == []                       # restored


def test_validate_catches_an_unmapped_rule():
    mod = _load()
    saved = list(mod.RULES)
    try:
        mod.RULES.append((len(mod.RULES) + 1, "rogue-unmapped-rule", "not-a-guard"))
        errs = mod.validate()
        assert any("unmapped" in e for e in errs)     # DEV_LOOP R62
    finally:
        mod.RULES[:] = saved
    assert mod.validate() == []                       # restored


# ── #348 S15: delivery-rule re-mapping + merge-go Default-B correction ──
def test_phase2b_delivery_rules_map_to_real_guards_not_placeholders():
    mod = _load()
    by_id = {rid: guard for (rid, _name, guard) in mod.RULES}
    # the new Phase-2b delivery guards exist (design entries, owner = building sub-issue)
    for g in ("deliver-gate", "deliver-go", "delivered-pending", "epic-completion", "upstream-roundtrip"):
        assert g in mod.GUARDS, g
    # the listed delivery rules now point at real guards, not phase-2/out-of-scope-v1 placeholders
    expect = {40: "epic-completion", 41: "deliver-gate", 42: "deliver-go", 43: "delivered-pending",
              45: "epic-completion", 49: "upstream-roundtrip", 51: "upstream-roundtrip",
              52: "upstream-roundtrip", 53: "delivered-pending", 54: "upstream-roundtrip",
              13: "pr-anchored"}
    for rid, guard in expect.items():
        assert by_id[rid] == guard, f"rule {rid} -> {by_id[rid]!r}, expected {guard!r}"
        assert by_id[rid] not in mod._NON_GUARD                    # never a placeholder
    # negative: a non-delivery rule mapping is untouched (still a placeholder where intended)
    assert by_id[46] == "deferred"                                 # size-routing stays deferred


def test_merge_go_desc_reflects_default_B_human_merge():
    mod = _load()
    desc = mod.GUARDS["merge-go"]["desc"]
    assert "HUMAN merge" in desc and "Default-B" in desc
    assert "EXECUTED by driver" not in desc                        # the stale per-unit-driver-merge claim is gone


# ── #348 S6: the Test-PyPI delivery target descriptor ──
def test_test_pypi_target_is_a_dry_delivery_descriptor():
    mod = _load()
    assert "test-pypi" in mod.TARGETS
    tp, cm = mod.TARGETS["test-pypi"], mod.TARGETS["core-monorepo"]
    # DRY: same monorepo/gates/DoD as core-monorepo, differing ONLY in the release index (no field drift)
    assert tp["repo"] == cm["repo"] and tp["gate_profile"] == cm["gate_profile"]
    assert tp["dod_profile"] == cm["dod_profile"]
    assert tp["release_index"] == "testpypi" and cm["release_index"] == "pypi"
    assert mod.RELEASE_INDEX_URLS["testpypi"].startswith("https://test.pypi.org")
    # #397 S14c: differs ALSO in the SEPARATE push/release repo (push- + index-isolation for the proof)
    assert tp["release_repo"] == "GrokBuildMJW/ironclad-testpypi"
    assert cm["release_repo"] == "GrokBuildMJW/ironclad" and tp["release_repo"] != cm["release_repo"]


def test_validate_catches_an_unknown_release_index():
    mod = _load()
    saved = dict(mod.TARGETS)
    try:
        mod.TARGETS["bad"] = {**mod.TARGETS["core-monorepo"], "release_index": "nope"}
        assert any("release_index" in e and "nope" in e for e in mod.validate())
    finally:
        mod.TARGETS.clear(); mod.TARGETS.update(saved)
