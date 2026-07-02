"""#994-S6 (C0-6): local rollback wheel-cache selection — pure, offline. The primary recovery tier: roll the
running instance back to the cached last-good wheel without touching PyPI.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_RB = _REPO / "scripts" / "ci" / "rollback.py"

pytestmark = pytest.mark.skipif(not _RB.is_file(), reason="private CI rollback.py absent — installed tree")


def _load():
    spec = importlib.util.spec_from_file_location("_rollback", _RB)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_wheel_version_parses_and_ignores_non_wheels():
    rb = _load()
    assert rb.wheel_version("ironclad_ai-0.0.22-py3-none-any.whl") == "0.0.22"
    assert rb.wheel_version("ironclad_ai-0.0.23rc1-py3-none-any.whl") == "0.0.23rc1"
    assert rb.wheel_version("notes.txt") == "" and rb.wheel_version("ironclad_ai.tar.gz") == ""


def test_last_good_wheel_picks_newest_older_than_current():
    rb = _load()
    cache = ["ironclad_ai-0.0.20-py3-none-any.whl", "ironclad_ai-0.0.21-py3-none-any.whl",
             "ironclad_ai-0.0.22-py3-none-any.whl", "readme.md"]
    # the bad release is 0.0.22 → roll back to 0.0.21 (newest STRICTLY older)
    assert rb.last_good_wheel(cache, current_version="0.0.22") == "ironclad_ai-0.0.21-py3-none-any.whl"
    # no current → newest cached
    assert rb.last_good_wheel(cache) == "ironclad_ai-0.0.22-py3-none-any.whl"
    # nothing older than the oldest → '' (fail-soft, no wheel to roll back to)
    assert rb.last_good_wheel(cache, current_version="0.0.20") == ""
    assert rb.last_good_wheel([]) == ""


def test_rollback_pip_argv_pins_the_local_wheel_no_index():
    rb = _load()
    argv = rb.rollback_pip_argv("/py", "/cache", "ironclad_ai-0.0.21-py3-none-any.whl")
    assert argv[:5] == ["/py", "-m", "pip", "install", "--no-index"]
    assert "--force-reinstall" in argv and argv[-1].endswith("ironclad_ai-0.0.21-py3-none-any.whl")
