"""#971 (epic #927): the english-only-export guard (scripts/ci/check_english_only_export.py).

Skips in the export/clean-room tree where scripts/ci is not present (core-only build).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_GUARD = _ROOT / "scripts" / "ci" / "check_english_only_export.py"

pytestmark = pytest.mark.skipif(not _GUARD.exists(), reason="scripts/ci absent (clean-room core-only)")


def _load():
    spec = importlib.util.spec_from_file_location("check_english_only_export", _GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_live_export_tree_is_english_only():
    assert _load().scan() == []          # the merged tree carries no un-allowlisted German


def test_hits_flag_german_and_ignore_english():
    g = _load()
    assert g._german_hits("Der Befehl wird nicht ausgeführt.")     # umlaut + stopwords
    assert g._german_hits("this uses ä somewhere")                # a lone umlaut is enough
    assert g._german_hits("das ist nicht gut")                     # >=2 stopwords, no umlaut
    assert not g._german_hits("The command was not executed.")     # plain English
    assert not g._german_hits("die()  # kill the process")         # a single English-ish 'die' is not enough


def test_allowlist_covers_the_deliberate_exceptions_only():
    g = _load()
    assert g._allowlisted("engine/messages.py")               # de overlay
    assert g._allowlisted("skills/mpr/locales/de.json")       # de overlay
    assert g._allowlisted("skills/mpr/eval/sets/regulatory.jsonl")   # corpus fixture
    assert g._allowlisted("skills/mpr/router.py")             # German-input feature file
    assert g._allowlisted("clients/ink/test/classify.test.ts")     # ink test (singular dir)
    assert g._allowlisted("ack/tests/test_x.py")              # python tests
    assert not g._allowlisted("engine/app.py")                # a normal engine file is NOT exempt
    assert not g._allowlisted("skills/mpr/entry.py")          # a normal MPR file is NOT exempt
