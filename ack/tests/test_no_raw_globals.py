"""Unit tests for the private no-raw-globals CI lint.

Loads scripts/ci/check_no_raw_globals.py by path and skips when absent (installed/clean-room tree).
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_LINT = _REPO / "scripts" / "ci" / "check_no_raw_globals.py"

pytestmark = pytest.mark.skipif(
    not _LINT.is_file(),
    reason="private CI lint (scripts/ci/check_no_raw_globals.py) absent - installed/clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_no_raw_globals", _LINT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _gx10(src):
    m = _load()
    v = m._Gx10Visitor()
    v.visit(ast.parse(src))
    return v


def _xmod(src):
    m = _load()
    v = m._CrossModuleVisitor()
    v.visit(ast.parse(src))
    return v


def test_current_tree_passes():
    assert _load().run() == 0  # the real engine tree is clean


def test_flags_raw_global_in_disallowed_function():
    v = _gx10("def helper():\n    return STATE_ROOT\n")
    assert any(sym == "STATE_ROOT" for (_ln, sym, _fn) in v.violations)


def test_allows_global_in_its_accessor():
    v = _gx10("def state_root():\n    return STATE_ROOT\n")
    assert v.violations == []


def test_allows_module_level_definition():
    v = _gx10("STATE_ROOT = '.ironclad'\n")
    assert v.violations == []


def test_allows_warm_session_in_active_accessor_only():
    assert _gx10("def _active_warm_session():\n    return WARM_SESSION_ID\n").violations == []
    assert _gx10("def other():\n    return WARM_SESSION_ID\n").violations


def test_cross_module_flags_attribute_read():
    v = _xmod("x = gx10.WARM_SESSION_ID\n")
    assert any(sym == "WARM_SESSION_ID" for (_ln, sym) in v.violations)


def test_cross_module_allows_accessor_call():
    v = _xmod("x = gx10.state_root()\n")
    assert v.violations == []


# --- hardening regression tests ---


def test_flags_module_level_read():
    v = _gx10("x = STATE_ROOT\n")
    assert v.violations


def test_allows_module_level_store_definition():
    v = _gx10("STATE_ROOT = '.ironclad'\n")
    assert v.violations == []


def test_flags_nested_function_in_accessor():
    src = "def state_root():\n    def leak():\n        return STATE_ROOT\n    return leak\n"
    assert _gx10(src).violations


def test_init_is_qualified_to_gx10_class():
    assert _gx10("class GX10:\n    def __init__(self):\n        return _MEMORY_CONFIG\n").violations == []
    assert _gx10("class Other:\n    def __init__(self):\n        return _MEMORY_CONFIG\n").violations


def test_cross_module_flags_from_import():
    v = _xmod("from gx10 import WARM_SESSION_ID\n")
    assert any(sym == "WARM_SESSION_ID" for (_ln, sym) in v.violations)


def test_cross_module_flags_getattr_literal():
    assert _xmod('x = getattr(gx10, "STATE_ROOT")\n').violations
    assert _xmod('x = getattr(gx10, "not_a_target")\n').violations == []



def test_flags_default_arg_in_accessor_signature():
    assert _gx10("def state_root(x=STATE_ROOT):\n    return x\n").violations


def test_flags_decorator_referencing_target():
    assert _gx10("@deco(STATE_ROOT)\ndef state_root():\n    return 1\n").violations


def test_flags_module_level_augassign():
    assert _gx10('STATE_ROOT += "x"\n').violations
    assert _gx10('STATE_ROOT = ".ironclad"\n').violations == []


def test_cross_module_flags_fully_qualified_gx10_path():
    assert _xmod("y = core.engine.gx10.STATE_ROOT\n").violations            # dotted attribute access
    assert _xmod('y = getattr(core.engine.gx10, "WARM_SESSION_ID")\n').violations  # dotted getattr
    assert _xmod("y = gx10.state_root()\n").violations == []                # accessor call still fine


def _xmod_full(src):
    m = _load()
    t = ast.parse(src)
    v = m._CrossModuleVisitor(aliases=m._gx10_aliases(t))
    v.visit(t)
    return v


def test_cross_module_flags_import_as_alias():
    assert _xmod_full("import core.engine.gx10 as g\ny = g.STATE_ROOT\n").violations          # import ... as alias
    assert _xmod_full("import gx10 as g\ny = g.WARM_SESSION_ID\n").violations                # bare import as alias
    assert _xmod_full("import gx10 as g\ny = g.state_root()\n").violations == []             # accessor via alias is fine


def test_cross_module_flags_star_import():
    assert _xmod_full("from core.engine.gx10 import *\n").violations                          # star import bare-exposes targets
    assert _xmod_full("from gx10 import *\n").violations


def test_cross_module_flags_from_import_module_alias():
    assert _xmod_full("from core.engine import gx10 as g\ny = g.STATE_ROOT\n").violations       # from-pkg import module as alias
    assert _xmod_full("from core.engine import gx10\ny = gx10.WARM_SESSION_ID\n").violations    # from-pkg import module (bare)
    assert _xmod_full("from core.engine import gx10 as g\ny = g.state_root()\n").violations == []  # accessor via alias is fine
