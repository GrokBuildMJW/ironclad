"""Extension SDK surface (ADR-0004, #72) — the curated `ack.sdk` contract a separate repo builds
against. These tests pin the public surface: every advertised name resolves, the surface is
importable in isolation (a plugin author imports only `ack.sdk`), and the re-exported gate behaves
identically to `ack.gate` (so validating a plugin via the SDK == the gate Ironclad runs).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ack import sdk


# The contract as of ADR-0004 D2. A change here is a public-API change (CHANGELOG-worthy).
_EXPECTED = {
    # tool kind
    "Registry", "Registration", "RegistrationKind", "RegistryError",
    "DuplicateRegistrationError", "derive_tool_schema", "get_registry", "tool", "task_type",
    # playbook kind
    "Playbook", "PlaybookError", "parse_playbook", "discover_playbooks",
    # prompt kind
    "Prompt", "PromptError", "Variable", "parse_prompt", "discover_prompts",
    "assemble", "run_prompt", "AssemblyError",
    # gate
    "gate", "gate_tool", "gate_playbook", "gate_prompt", "GateResult",
    # i18n
    "Localizer",
    # catalogue
    "Catalogue", "SkillEntry", "build_catalogue", "install", "update",
}


def test_sdk_all_matches_contract():
    assert set(sdk.__all__) == _EXPECTED


def test_every_exported_name_resolves():
    missing = [n for n in sdk.__all__ if not hasattr(sdk, n)]
    assert not missing, f"advertised but unresolvable: {missing}"


def test_no_duplicate_exports():
    assert len(sdk.__all__) == len(set(sdk.__all__))


def test_sdk_is_the_same_objects_as_internal_modules():
    # the SDK is a thin re-export, not a copy — identity must hold so behavior can't drift
    from ack import gate as gate_mod, registry as registry_mod
    assert sdk.gate is gate_mod.gate
    assert sdk.Registry is registry_mod.Registry
    assert sdk.derive_tool_schema is registry_mod.derive_tool_schema


def test_gate_via_sdk_validates_a_plugin(tmp_path):
    # what a separate-repo author does: validate their plugin with the SDK gate before shipping
    from ack import skillgen
    skillgen.write_scaffold(
        skillgen.SkillSpec(capability="greet", description="Greet", kind="tool",
                           params=[("name", "str")]), tmp_path, force=True)
    res = sdk.gate(tmp_path / "skills" / "greet.py")
    assert res.passed and res.kind == "tool", res.reasons


def test_derive_tool_schema_via_sdk():
    def run(name: str, count: int = 1) -> str:  # noqa: ARG001
        return name
    schema = sdk.derive_tool_schema(run)
    assert schema["type"] == "object"
    assert "name" in schema["properties"] and "name" in schema["required"]
    assert "count" not in schema.get("required", [])   # has a default → optional


def test_assemble_via_sdk(tmp_path):
    d = tmp_path / "skills" / "p"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\ncapability: p\nkind: prompt\ndescription: d\nvariables: [x]\nrequired: [x]\n---\nHi {x}.\n",
        encoding="utf-8")
    prompt = sdk.parse_prompt(d / "SKILL.md")
    assert sdk.assemble(prompt, {"x": "Ada"}) == "Hi Ada."
