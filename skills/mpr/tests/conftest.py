"""Pytest bootstrap for the MPR plugin test suite (private — outside core/).

Mirrors the ACK test convention (``Path(__file__).resolve().parents[N]`` + ``sys.path.insert``) and
anchors three roots so the tests run regardless of the invocation directory:

* ``skills/``      → ``import mpr.schema`` / ``mpr.router`` / ``mpr.registry`` (the plugin package)
* ``engine/`` → ``import router`` / ``providers`` / ``dispatch`` (the P0 provider-router MPR rides)
* ``core/``        → ``import ack`` (registry/case primitives the plugin consumes)

The plugin itself is loaded standalone by ironclad's discovery (``spec_from_file_location``,
registry.py:389) — these path anchors are a *test-time* convenience only; runtime path bootstrap
lives in the standalone entry ``skills/mpr/skills/mpr_research.py``.
"""
import sys
from pathlib import Path

import pytest

# skills/mpr/tests/conftest.py → parents: [0]=tests [1]=mpr [2]=skills [3]=repo-root
_SKILLS = Path(__file__).resolve().parents[2]            # skills/   → import mpr.*
_ROOT = Path(__file__).resolve().parents[3]              # repo root
# Layout-agnostic: the private monorepo nests the public tree under core/ (core/engine, core/ack);
# the OSS export flattens it (engine/ + ack/ at the root). Resolve both so the MPR suite runs in
# either tree without edits — the plugin ships public with working tests.
_ENGINE = (_ROOT / "core" / "engine") if (_ROOT / "core" / "engine").is_dir() else (_ROOT / "engine")
_CORE = (_ROOT / "core") if (_ROOT / "core").is_dir() else _ROOT

for _p in (_SKILLS, _ENGINE, _CORE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


@pytest.fixture(autouse=True)
def _reset_render_language():
    """The template renderers read a module-global render language (i18n._ACTIVE_LANG, set by
    synthesize()). A test that synthesizes in ``de`` would otherwise leak that into later
    template-direct tests. Reset to the English default before AND after each test (#44)."""
    from mpr import i18n
    i18n.use_language("en")
    yield
    i18n.use_language("en")
