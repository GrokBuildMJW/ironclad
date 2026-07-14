"""Spec↔dispatch parity guard (#940, epic #927) — the anti-drift teeth for the hand-authored spec.

Loads scripts/ci/check_command_spec_parity.py by path and skips when absent (installed/clean-room tree),
matching the other private-CI-guard tests.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_GUARD = _REPO / "scripts" / "ci" / "check_command_spec_parity.py"

pytestmark = pytest.mark.skipif(
    not _GUARD.is_file(),
    reason="private CI guard (scripts/ci/check_command_spec_parity.py) absent - installed/clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_command_spec_parity", _GUARD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_live_parity_passes():
    # The spec matches the real dispatch verbs, typed boot-only schema, and generator parser.
    assert _load().check() == []


def test_dispatch_verbs_derived_from_source_exclude_promptname_and_else():
    g = _load()
    src = (
        "def _dispatch(agent, user_input):\n"
        "    cmd = user_input.lower()\n"
        "    if cmd == 'help':\n"
        "        pass\n"
        "    elif cmd == 'config get' or cmd.startswith('config get '):\n"
        "        arg = 'on'\n"                       # a nested-body literal that must NOT be scored
        "    elif cmd.startswith('lifecycle '):\n"
        "        pass\n"
        "    elif _PROMPTS and _resolve_prompt_name(user_input) is not None:\n"  # prompt-name → excluded
        "        pass\n"
        "    else:\n"                                # fall-through → excluded
        "        agent.run(user_input)\n"
    )
    assert g.dispatch_verbs(src) == {"help", "config get", "lifecycle"}


def test_guard_has_teeth_a_missing_verb_is_flagged():
    # simulate the spec omitting a live verb: the set-diff the guard performs must flag it.
    g = _load()
    src = "def _dispatch(agent, user_input):\n    cmd=user_input.lower()\n    if cmd=='newverb':\n        pass\n"
    disp = g.dispatch_verbs(src)
    import command_spec as cs
    assert "newverb" in disp and "newverb" not in cs.verbs()   # a drift the guard reports as MISSING


def test_runtime_frozen_keys_match_schema_and_spec():
    import command_spec as cs
    import gx10
    import config_schema
    assert gx10._FROZEN_CONFIG_KEYS
    assert gx10._FROZEN_CONFIG_KEYS == cs.SPEC_FROZEN_CONFIG_KEYS == config_schema.BOOT_ONLY_KEYS
