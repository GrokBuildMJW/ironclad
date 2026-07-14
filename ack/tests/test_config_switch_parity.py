"""F9 generated config documentation and switch/read parity guard contract."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[3]
_GUARD = _REPO / "scripts" / "ci" / "check_config_switch_parity.py"
_GENERATOR = _REPO / "scripts" / "ci" / "gen_config_runtime.py"

pytestmark = pytest.mark.skipif(
    not (_GUARD.is_file() and _GENERATOR.is_file()),
    reason="private CI config-doc guards are absent from an installed/clean-room tree",
)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_live_schema_documented_read_and_boot_only_inventories_match():
    guard = _load(_GUARD, "_config_switch_parity")
    assert guard.check(_REPO) == []


def test_generated_config_runtime_regions_are_byte_exact_and_deterministic():
    generator = _load(_GENERATOR, "_config_runtime_generator")
    document = generator.DOCUMENT.read_text(encoding="utf-8")
    assert generator.generate_document(document) == document
    assert generator.generate_document(document) == generator.generate_document(document)


def test_three_empty_switch_sets_cannot_pass_and_known_switch_is_required():
    guard = _load(_GUARD, "_config_switch_non_vacuity")
    problems = guard.set_parity_problems(set(), set(), set(), set())
    assert any("non-vacuity" in problem for problem in problems)
    assert any(guard.KNOWN_SWITCH in problem for problem in problems)


def test_documented_tombstone_is_rejected_as_a_live_switch():
    guard = _load(_GUARD, "_config_switch_tombstone")
    live = {guard.KNOWN_SWITCH}
    documented = {guard.KNOWN_SWITCH, guard.KNOWN_TOMBSTONE}
    problems = guard.set_parity_problems(live, documented, live, {guard.KNOWN_TOMBSTONE})
    assert any(guard.KNOWN_TOMBSTONE in problem and "TOMBSTONE" in problem for problem in problems)


def test_synthetic_raw_boolean_read_is_flagged_and_unknown_is_not_live():
    guard = _load(_GUARD, "_config_switch_raw_read")
    schema = guard._load_schema(_REPO / "core" / "engine" / "config_schema.py")
    source = "def choose(cfg):\n    if bool(cfg.get('hidden_protection')):\n        return 'off-branch'\n"
    scan = guard.scan_source(
        source, leaves=schema.LEAVES, tombstones=schema.TOMBSTONES,
    )
    assert scan.raw_violations
    assert "hidden_protection" in scan.unknown_live_reads


def test_synthetic_nested_retired_enabled_read_is_flagged():
    guard = _load(_GUARD, "_config_switch_retired_read")
    schema = guard._load_schema(_REPO / "core" / "engine" / "config_schema.py")
    source = "def choose(cfg):\n    if cfg.get('audit', {}).get('enabled'):\n        return 'bypass'\n"
    scan = guard.scan_source(
        source, leaves=schema.LEAVES, tombstones=schema.TOMBSTONES,
    )
    assert any("audit.enabled" in problem and "retired" in problem for problem in scan.raw_violations)


def test_ifexp_root_alias_cannot_hide_a_retired_switch_gate():
    guard = _load(_GUARD, "_config_switch_ifexp_alias")
    schema = guard._load_schema(_REPO / "core" / "engine" / "config_schema.py")
    source = """\
def choose():
    active = _EFFECTIVE_CFG if _EFFECTIVE_CFG is not None else {}
    if active.get("audit", {}).get("enabled"):
        return "off-branch"
"""
    scan = guard.scan_source(
        source, leaves=schema.LEAVES, tombstones=schema.TOMBSTONES,
    )
    assert any("audit.enabled" in problem and "retired" in problem for problem in scan.raw_violations)


def test_module_dict_cannot_hide_a_known_tombstone_gate():
    guard = _load(_GUARD, "_config_switch_module_dict")
    schema = guard._load_schema(_REPO / "core" / "engine" / "config_schema.py")
    source = """\
_SIDE_CFG = {}
if _SIDE_CFG.get("audit", {}).get("enabled"):
    selected = "off-branch"
"""
    scan = guard.scan_source(
        source, leaves=schema.LEAVES, tombstones=schema.TOMBSTONES,
    )
    assert any(
        "unknown operational config gate" in problem and "_SIDE_CFG.audit.enabled" in problem
        for problem in scan.raw_violations
    )


@pytest.mark.parametrize(
    "source",
    (
        "selected = [row for row in rows if row.get('audit', {}).get('enabled')]\n",
        "selected = lambda row: row.get('audit', {}).get('enabled')\n",
    ),
)
def test_unresolved_known_tombstone_gates_in_comprehensions_and_lambdas_fail_closed(source):
    guard = _load(_GUARD, "_config_switch_expression_gate")
    schema = guard._load_schema(_REPO / "core" / "engine" / "config_schema.py")
    scan = guard.scan_source(
        source, leaves=schema.LEAVES, tombstones=schema.TOMBSTONES,
    )
    assert any("unknown operational config gate" in problem for problem in scan.raw_violations)


def test_unresolved_non_config_enabled_path_is_ignored():
    guard = _load(_GUARD, "_config_switch_non_config_path")
    schema = guard._load_schema(_REPO / "core" / "engine" / "config_schema.py")
    source = "selected = [r for r in rows if r.get('meta', {}).get('enabled')]\n"
    scan = guard.scan_source(
        source, leaves=schema.LEAVES, tombstones=schema.TOMBSTONES,
    )
    assert scan.raw_violations == []


def test_walrus_root_alias_cannot_hide_a_retired_switch_gate():
    guard = _load(_GUARD, "_config_switch_walrus_alias")
    schema = guard._load_schema(_REPO / "core" / "engine" / "config_schema.py")
    source = """\
def choose():
    if (c := _EFFECTIVE_CFG).get("audit", {}).get("enabled"):
        return "off-branch"
"""
    scan = guard.scan_source(
        source, leaves=schema.LEAVES, tombstones=schema.TOMBSTONES,
    )
    assert any("audit.enabled" in problem and "retired" in problem for problem in scan.raw_violations)


def test_match_subject_cannot_hide_a_retired_switch_gate():
    guard = _load(_GUARD, "_config_switch_match_subject")
    schema = guard._load_schema(_REPO / "core" / "engine" / "config_schema.py")
    source = """\
def choose(cfg):
    match cfg.get("audit", {}).get("enabled"):
        case True:
            return "off-branch"
"""
    scan = guard.scan_source(
        source, leaves=schema.LEAVES, tombstones=schema.TOMBSTONES,
    )
    assert any("audit.enabled" in problem and "retired" in problem for problem in scan.raw_violations)


def test_legitimate_rooted_switch_read_remains_typed_and_allowed():
    guard = _load(_GUARD, "_config_switch_rooted_control")
    schema = guard._load_schema(_REPO / "core" / "engine" / "config_schema.py")
    source = """\
def choose(cfg):
    if cfg.get("search", {}).get("enabled"):
        return "enabled"
"""
    scan = guard.scan_source(
        source, leaves=schema.LEAVES, tombstones=schema.TOMBSTONES,
    )
    assert scan.raw_violations == []
    assert scan.read_switches == {"search.enabled"}
