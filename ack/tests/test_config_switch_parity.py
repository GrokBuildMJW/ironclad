"""F9 generated config documentation and switch/read parity guard contract."""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

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


@pytest.mark.parametrize(
    "gate",
    (
        'getattr(cfg.get("audit", {}), "get")("enabled")',
        'get_config().get("audit", {}).get("enabled")',
        'itemgetter("enabled")(cfg["audit"])',
    ),
)
def test_alternate_get_chains_cannot_hide_a_retired_switch_gate(gate):
    guard = _load(_GUARD, f"_config_switch_alternate_get_{abs(hash(gate))}")
    schema = guard._load_schema(_REPO / "core" / "engine" / "config_schema.py")
    source = f"def choose(cfg):\n    if {gate}:\n        return 'bypass'\n"
    scan = guard.scan_source(
        source, leaves=schema.LEAVES, tombstones=schema.TOMBSTONES,
    )
    assert any("audit.enabled" in problem for problem in scan.raw_violations)


def test_getattr_get_live_switch_resolves_without_a_violation():
    guard = _load(_GUARD, "_config_switch_getattr_live")
    schema = guard._load_schema(_REPO / "core" / "engine" / "config_schema.py")
    source = "def choose(cfg):\n    return getattr(cfg, 'get')('search').get('enabled')\n"
    scan = guard.scan_source(
        source, leaves=schema.LEAVES, tombstones=schema.TOMBSTONES,
    )
    assert scan.raw_violations == []
    assert scan.read_switches == {"search.enabled"}


def test_getattr_get_with_default_cannot_hide_a_retired_switch_read():
    guard = _load(_GUARD, "_config_switch_getattr_default_tombstone")
    schema = guard._load_schema(_REPO / "core" / "engine" / "config_schema.py")
    source = "def choose(cfg):\n    return getattr(cfg, 'get', None)('audit', {}).get('enabled')\n"
    scan = guard.scan_source(
        source, leaves=schema.LEAVES, tombstones=schema.TOMBSTONES,
    )
    assert any("audit.enabled" in problem and "retired" in problem for problem in scan.raw_violations)


def test_getattr_get_with_default_live_switch_resolves_without_a_violation():
    guard = _load(_GUARD, "_config_switch_getattr_default_live")
    schema = guard._load_schema(_REPO / "core" / "engine" / "config_schema.py")
    source = "def choose(cfg):\n    return getattr(cfg, 'get', None)('search', {}).get('enabled')\n"
    scan = guard.scan_source(
        source, leaves=schema.LEAVES, tombstones=schema.TOMBSTONES,
    )
    assert scan.raw_violations == []
    assert scan.read_switches == {"search.enabled"}


def test_resolvable_tombstone_read_is_flagged_before_a_single_key_rebind_gate():
    guard = _load(_GUARD, "_config_switch_rebound_tombstone")
    schema = guard._load_schema(_REPO / "core" / "engine" / "config_schema.py")
    source = """\
def choose(cfg):
    sec = {"enabled": cfg.get("audit", {}).get("enabled")}
    if sec["enabled"]:
        return "bypass"
"""
    scan = guard.scan_source(
        source, leaves=schema.LEAVES, tombstones=schema.TOMBSTONES,
    )
    assert sum("audit.enabled" in problem for problem in scan.raw_violations) == 1
    assert any("raw operational config read" in problem for problem in scan.raw_violations)


@pytest.mark.parametrize(
    "body",
    (
        'v = get_config().get("audit", {}).get("enabled")',
        'return get_config().get("audit", {}).get("enabled")',
    ),
)
def test_call_rooted_non_gate_reads_cannot_hide_a_retired_switch(body):
    guard = _load(_GUARD, f"_config_switch_call_rooted_tombstone_{abs(hash(body))}")
    schema = guard._load_schema(_REPO / "core" / "engine" / "config_schema.py")
    source = f"def choose():\n    {body}\n"
    scan = guard.scan_source(
        source, leaves=schema.LEAVES, tombstones=schema.TOMBSTONES,
    )
    assert any(
        "raw operational config read 'audit.enabled' is retired" in problem
        for problem in scan.raw_violations
    )


def test_call_rooted_non_gate_live_switch_does_not_create_a_raw_violation():
    guard = _load(_GUARD, "_config_switch_call_rooted_live")
    schema = guard._load_schema(_REPO / "core" / "engine" / "config_schema.py")
    source = 'def choose():\n    v = get_config().get("search", {}).get("enabled")\n'
    scan = guard.scan_source(
        source, leaves=schema.LEAVES, tombstones=schema.TOMBSTONES,
    )
    assert scan.raw_violations == []
    assert scan.unknown_live_reads == set()


def test_string_constant_key_resolves_a_retired_switch_gate():
    guard = _load(_GUARD, "_config_switch_constant_key")
    schema = guard._load_schema(_REPO / "core" / "engine" / "config_schema.py")
    source = "def choose(cfg):\n    k = 'enabled'\n    if cfg.get('audit', {}).get(k):\n        return 'bypass'\n"
    scan = guard.scan_source(
        source, leaves=schema.LEAVES, tombstones=schema.TOMBSTONES,
    )
    assert any("audit.enabled" in problem for problem in scan.raw_violations)


def test_walrus_key_cannot_hide_a_retired_switch_read():
    guard = _load(_GUARD, "_config_switch_walrus_key_tombstone")
    schema = guard._load_schema(_REPO / "core" / "engine" / "config_schema.py")
    source = 'def choose(cfg):\n    return cfg.get("audit", {}).get(k := "enabled")\n'
    scan = guard.scan_source(
        source, leaves=schema.LEAVES, tombstones=schema.TOMBSTONES,
    )
    assert any("audit.enabled" in problem and "retired" in problem for problem in scan.raw_violations)


def test_walrus_key_live_switch_resolves_without_a_violation():
    guard = _load(_GUARD, "_config_switch_walrus_key_live")
    schema = guard._load_schema(_REPO / "core" / "engine" / "config_schema.py")
    source = 'def choose(cfg):\n    return cfg.get("search", {}).get(k := "enabled")\n'
    scan = guard.scan_source(
        source, leaves=schema.LEAVES, tombstones=schema.TOMBSTONES,
    )
    assert scan.raw_violations == []
    assert scan.read_switches == {"search.enabled"}


def test_ambiguous_non_config_key_does_not_create_a_false_positive():
    guard = _load(_GUARD, "_config_switch_ambiguous_key")
    schema = guard._load_schema(_REPO / "core" / "engine" / "config_schema.py")
    source = """\
def choose(rows, use_enabled):
    k = "enabled"
    if not use_enabled:
        k = "visible"
    return [row for row in rows if row.get("audit", {}).get(k)]
"""
    scan = guard.scan_source(
        source, leaves=schema.LEAVES, tombstones=schema.TOMBSTONES,
    )
    assert scan.raw_violations == []


def test_augmented_string_key_is_unresolved_without_a_false_positive():
    guard = _load(_GUARD, "_config_switch_augmented_key")
    schema = guard._load_schema(_REPO / "core" / "engine" / "config_schema.py")
    source = """\
def choose(cfg):
    k = "enabled"
    k += "_x"
    value = cfg.get("audit", {}).get(k)
    if cfg.get("audit", {}).get(k):
        return value
"""
    scan = guard.scan_source(
        source, leaves=schema.LEAVES, tombstones=schema.TOMBSTONES,
    )
    assert scan.raw_violations == []
    assert scan.unknown_live_reads == set()


def test_display_only_function_still_reports_retired_config_reads():
    guard = _load(_GUARD, "_config_switch_display_tombstone")
    schema = guard._load_schema(_REPO / "core" / "engine" / "config_schema.py")
    source = "def _render_config(cfg):\n    return cfg.get('audit', {}).get('enabled')\n"
    scan = guard.scan_source(
        source, leaves=schema.LEAVES, tombstones=schema.TOMBSTONES,
    )
    assert any("audit.enabled" in problem and "retired" in problem for problem in scan.raw_violations)


def test_display_only_non_switch_read_is_ignored_and_does_not_count_for_parity():
    guard = _load(_GUARD, "_config_switch_display_non_switch")
    schema = guard._load_schema(_REPO / "core" / "engine" / "config_schema.py")
    source = """\
def _empty_pipeline_hint(cfg):
    return bool(cfg.get("paths", {}).get("active_capability_backlog"))
"""
    scan = guard.scan_source(
        source, leaves=schema.LEAVES, tombstones=schema.TOMBSTONES,
    )
    assert scan.raw_violations == []
    assert scan.read_switches == set()
    assert scan.unknown_live_reads == set()


def test_real_schema_external_seams_match_memory_and_warm_environment_bindings():
    guard = _load(_GUARD, "_config_switch_real_external_seams")
    schema = guard._load_schema(_REPO / "core" / "engine" / "config_schema.py")
    assert guard.external_seam_problems(schema) == []


def test_undeclared_memory_environment_binding_is_rejected():
    guard = _load(_GUARD, "_config_switch_external_seam_drift")
    schema = guard._load_schema(_REPO / "core" / "engine" / "config_schema.py")
    leaves = dict(schema.LEAVES)
    leaves["memory.base_url"] = replace(
        leaves["memory.base_url"],
        env_names=(*leaves["memory.base_url"].env_names, "GX10_MEMORY_UNDECLARED"),
    )
    synthetic = SimpleNamespace(
        EXTERNAL_SEAMS=schema.EXTERNAL_SEAMS,
        LEAVES=leaves,
        SWITCH=schema.SWITCH,
    )
    problems = guard.external_seam_problems(synthetic)
    assert any("GX10_MEMORY_UNDECLARED" in problem for problem in problems)


def test_schema_derived_external_seam_region_matches_live_document_bytes():
    generator = _load(_GENERATOR, "_config_runtime_external_seams")
    schema = generator._load_schema()
    document = generator.DOCUMENT.read_text(encoding="utf-8")
    start = document.index(generator.EXTERNAL_BEGIN)
    stop = document.index(generator.EXTERNAL_END, start) + len(generator.EXTERNAL_END)
    assert generator.render_external_seams(schema) == document[start:stop]
