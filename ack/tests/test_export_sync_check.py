"""export-sync-check classification (#195, ADR-0007), offline.

Pins the pure `classify()` + the LF-normalisation: a hand-added public file is always drift; at the
same source commit the trees must be byte-identical; when main is ahead the extra/changed files are
expected (info, not failure); a CRLF-only difference is never drift. Lives in `scripts/ci/` (private)
-> skips in an installed/clean-room tree.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_ES = _REPO / "scripts" / "ci" / "export_sync_check.py"

pytestmark = pytest.mark.skipif(
    not _ES.is_file(),
    reason="private CI export-sync-check absent — installed/clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_exportsync", _ES)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_hand_added_public_file_is_drift_in_both_regimes():
    es = _load()
    ex = {"a.py": b"x"}
    pub = {"a.py": b"x", "evil.txt": b"hand-added"}
    # main-ahead regime
    f, _ = es.classify(ex, pub, source_sha="OLD", head_sha="HEAD")
    assert any("evil.txt" in x for x in f)
    # strict regime
    f2, _ = es.classify(ex, pub, source_sha="HEAD", head_sha="HEAD")
    assert any("evil.txt" in x for x in f2)


def test_strict_regime_requires_byte_identity():
    es = _load()
    ex = {"a.py": b"new", "added.py": b"z"}
    pub = {"a.py": b"old"}                       # content differs + a.py missing 'added.py'
    f, _ = es.classify(ex, pub, source_sha="SAME", head_sha="SAME")
    assert any("a.py" in x for x in f)           # content drift at same commit
    assert any("added.py" in x for x in f)       # dropped on push


def test_main_ahead_changes_are_info_not_failure():
    es = _load()
    ex = {"a.py": b"new", "added.py": b"z"}
    pub = {"a.py": b"old"}                       # main moved ahead of the published source
    f, info = es.classify(ex, pub, source_sha="OLDER", head_sha="HEAD")
    assert f == []                               # not drift — expected unreleased work
    assert info and "ahead" in info[0]


def test_stamp_file_itself_is_ignored():
    es = _load()
    ex = {"a.py": b"x"}
    pub = {"a.py": b"x", es.STAMP: b'{"commit":"abc"}'}   # the stamp differs/extra by design
    f, _ = es.classify(ex, pub, source_sha="X", head_sha="X")
    assert f == []


def test_norm_treats_crlf_and_lf_as_equal(tmp_path):
    es = _load()
    a = tmp_path / "a"; b = tmp_path / "b"
    a.write_bytes(b"line1\r\nline2\r\n"); b.write_bytes(b"line1\nline2\n")
    assert es._norm(a) == es._norm(b)            # CRLF-only difference is never drift
