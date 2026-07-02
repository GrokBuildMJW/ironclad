"""#939 (epic #927): the ink↔command_spec parity guard (scripts/ci/check_ink_command_parity.py).

Skips in the export/clean-room tree, where scripts/ci is not present (core-only build).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_GUARD = _ROOT / "scripts" / "ci" / "check_ink_command_parity.py"

pytestmark = pytest.mark.skipif(not _GUARD.exists(), reason="scripts/ci absent (clean-room core-only)")


def _load():
    spec = importlib.util.spec_from_file_location("check_ink_command_parity", _GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_live_ink_mirror_matches_the_command_spec():
    assert _load().check() == []          # the real ink ALIASES/UNSAFE equal the command-spec


def test_extractors_parse_the_ts_literals():
    g = _load()
    src = ("export const ALIASES: Readonly<Record<string, string>> = "
           "{ lg: 'lifecycle gate', pj: 'project' };\n"
           "const UNSAFE: ReadonlySet<string> = new Set(['project', 'ace']);")
    assert g.ink_aliases(src) == {"lg": "lifecycle gate", "pj": "project"}
    assert g.ink_unsafe(src) == {"project", "ace"}


def test_a_drift_is_caught():
    # a mirror that drops a costly verb from UNSAFE (or renames an alias) must not equal the spec set
    g = _load()
    drifted = "export const ALIASES = { lg: 'lifecycle gate' };\nconst UNSAFE = new Set(['project']);"
    import sys
    sys.path.insert(0, str(_ROOT / "core" / "engine"))
    import command_spec as cs
    assert g.ink_aliases(drifted) != dict(cs.ALIASES)              # missing aliases → drift
    assert g.ink_unsafe(drifted) != set(cs.unsafe_first_words())   # missing costly verbs → drift


def test_coverage_guard_flags_a_missing_server_verb():
    # #952: the coverage check must catch a spec verb missing from the ink server subset (the did-you-mean
    # blindness that shipped for lifecycle/fork/ace) — extractor + premise, no touch to the real file.
    g = _load()
    import sys
    sys.path.insert(0, str(_ROOT / "core" / "engine"))
    import command_spec as cs
    spec_first = {v.split()[0] for v in cs.verbs()}
    src = "\n".join(f"  {{name: '{v}', scope: 'server'}}," for v in sorted(spec_first - {"lifecycle"}))
    server = g.ink_command_names(src, "server")
    assert "lifecycle" in spec_first and "lifecycle" not in server
