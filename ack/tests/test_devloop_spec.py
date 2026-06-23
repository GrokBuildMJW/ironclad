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
    assert set(mod.TARGETS) == {"internal-plugin", "core-monorepo"}
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
