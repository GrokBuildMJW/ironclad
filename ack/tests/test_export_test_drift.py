"""Guard test for the export-test-drift lint (#845, follow-up to #843).

`check_export_test_drift.py` flags any exported `test_*.py` that references the private `scripts/` tree
without an absence guard (it would `FileNotFoundError` in the public export, as #843 did). The guard lives
in `scripts/ci/` (private, NOT exported), so this test **skips** on an installed / clean-room tree where
`scripts/ci/` is absent — the same idiom it enforces (and which keeps THIS file clean under its own lint).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]          # mjw_agentic/ (private monorepo root)
_GUARD = _REPO / "scripts" / "ci" / "check_export_test_drift.py"
_CORE = _REPO / "core"

pytestmark = pytest.mark.skipif(
    not _GUARD.is_file(),
    reason="private CI guard (scripts/ci/) absent — installed/clean-room tree, lint not applicable",
)


def _load():
    spec = importlib.util.spec_from_file_location("_export_test_drift_xcheck", _GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_real_core_tree_has_no_unguarded_scripts_test():
    # The shipped core/ test tree must be clean — every scripts/-touching test guards for absence
    # (the #843 fix added the is_file/skip guard; the sibling export tests already had skipif).
    mod = _load()
    assert mod.find_test_drift(_CORE) == []


def _write_test(dirpath: Path, body: str) -> Path:
    tdir = dirpath / "tests"
    tdir.mkdir(parents=True, exist_ok=True)
    f = tdir / "test_synthetic.py"
    f.write_text(body, encoding="utf-8")
    return f


def test_flags_an_unguarded_scripts_reference(tmp_path):
    mod = _load()
    _write_test(tmp_path, (
        "from pathlib import Path\n"
        "import importlib.util\n"
        "led = Path(__file__).resolve().parents[3] / 'scripts' / 'devprocess' / 'ledger.py'\n"
        "spec = importlib.util.spec_from_file_location('x', led)\n"
    ))
    violations = mod.find_test_drift(tmp_path)
    assert len(violations) == 1 and violations[0].name == "test_synthetic.py"


def test_is_file_guard_clears_the_reference(tmp_path):
    mod = _load()
    _write_test(tmp_path, (
        "from pathlib import Path\n"
        "import pytest\n"
        "led = Path(__file__).resolve().parents[3] / 'scripts' / 'devprocess' / 'ledger.py'\n"
        "if not led.is_file():\n"
        "    pytest.skip('absent', allow_module_level=True)\n"
    ))
    assert mod.find_test_drift(tmp_path) == []


def test_skipif_guard_clears_the_reference(tmp_path):
    mod = _load()
    _write_test(tmp_path, (
        "from pathlib import Path\n"
        "import pytest\n"
        "_S = Path(__file__).resolve().parents[3] / 'scripts' / 'ci' / 'x.py'\n"
        "pytestmark = pytest.mark.skipif(not _S.is_file(), reason='absent')\n"
    ))
    assert mod.find_test_drift(tmp_path) == []


def test_no_scripts_reference_is_ignored(tmp_path):
    # A test that never touches scripts/ is irrelevant to this lint (no guard required).
    mod = _load()
    _write_test(tmp_path, (
        "def test_ok():\n"
        "    assert 1 + 1 == 2\n"
    ))
    assert mod.find_test_drift(tmp_path) == []
