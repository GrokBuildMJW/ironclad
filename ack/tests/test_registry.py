"""Agent-Contract-Kernel — Registry / Discovery tests (KGC-554, ACK component 4).

Validation + regression guard for the kernel discovery surface: task-types, tools
(with auto-derived grammar-clean schemas), filesystem skill discovery, MCP binding
and unified capability resolution.

  python -m pytest ack/tests/test_registry.py -v
"""
from __future__ import annotations

import asyncio
import logging
import textwrap
from enum import Enum
from pathlib import Path
from typing import Optional

import pytest

from ack.registry import (
    DuplicateRegistrationError,
    Registration,
    RegistrationKind,
    Registry,
    RegistryError,
    derive_tool_schema,
    get_registry,
    task_type,
    tool,
)

# Re-exported from the spec package (single import surface).
# NOTE (adaptation): ``ack.__init__`` does NOT re-export ``Registry`` — it is imported
# from its concrete module instead. Intent (a single stable import surface) is preserved.
from ack.registry import Registry as RegistryFromSpec
from ack.case_spec import lint_schema_for_xgrammar


# --------------------------------------------------------------------------- #
# Task-types
# --------------------------------------------------------------------------- #
def test_register_and_resolve_task_type():
    r = Registry()

    class InvoiceChase:
        """Chase overdue invoices."""

    reg = r.register_task_type("invoice-chase", InvoiceChase)
    assert reg.kind is RegistrationKind.TASK_TYPE
    assert reg.cls is InvoiceChase
    assert reg.description == "Chase overdue invoices."

    resolved = r.resolve_capability("invoice-chase")
    assert resolved is not None and resolved.cls is InvoiceChase


def test_duplicate_task_type_is_fail_loud():
    r = Registry()

    class A:
        pass

    class B:
        pass

    r.register_task_type("dup", A)
    with pytest.raises(DuplicateRegistrationError):
        r.register_task_type("dup", B)


def test_task_type_decorator_targets_explicit_registry():
    r = Registry()

    @task_type("my-case", registry=r)
    class MyCase:
        pass

    # Decorator returns the class unchanged + registers it.
    assert MyCase.__name__ == "MyCase"
    assert r.resolve_capability("my-case").cls is MyCase


# --------------------------------------------------------------------------- #
# Tools + auto-schema
# --------------------------------------------------------------------------- #
def test_register_tool_auto_schema_required_and_optional():
    r = Registry()

    def lookup_customer(customer_id: str, include_orders: bool = False) -> dict:
        """Look a customer up."""
        return {}

    reg = r.register_tool("lookup_customer", lookup_customer)
    assert reg.kind is RegistrationKind.TOOL
    schema = reg.schema
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["customer_id"] == {"type": "string"}
    assert schema["properties"]["include_orders"] == {"type": "boolean"}
    # required = params without a default; optional ones are excluded.
    assert schema["required"] == ["customer_id"]
    assert reg.description == "Look a customer up."


def test_auto_schema_is_xgrammar_clean():
    # A list-typed param must NOT introduce minItems/maxItems/uniqueItems etc.
    def f(tags: list[str], count: int) -> None:
        ...

    schema = derive_tool_schema(f)
    assert lint_schema_for_xgrammar(schema) == [], "derived tool schema must be grammar-safe"
    assert schema["properties"]["tags"] == {"type": "array", "items": {"type": "string"}}
    assert schema["properties"]["count"] == {"type": "integer"}


def test_auto_schema_optional_not_required():
    def f(a: str, b: Optional[int] = None) -> None:
        ...

    schema = derive_tool_schema(f)
    assert schema["required"] == ["a"]
    assert "b" in schema["properties"]


def _pep604_handler(a: str, b: int | None = None) -> None:
    # ACK-1 (#503): module-level so get_type_hints resolves the PEP-604 `int | None` under
    # `from __future__ import annotations` — exactly how real tools are declared.
    ...


def test_auto_schema_pep604_optional_not_required():
    # ACK-1 (#503): `X | None` (types.UnionType) must be treated like Optional[X] — not a bare-string
    # required fallback. Pre-fix the schema gave the public SDK a wrong model-facing shape for `b`.
    schema = derive_tool_schema(_pep604_handler)
    assert schema["required"] == ["a"]                        # b is optional (X | None), not required
    assert schema["properties"]["b"] == {"type": "integer"}  # the non-None arm, not {"type": "string"}


class _Mode(str, Enum):
    FAST = "fast"
    SLOW = "slow"


def _enum_handler(self, identity, vessel_id, mode: _Mode, *args, **kwargs):  # noqa: ANN001
    # Module-level (not nested) so ``get_type_hints`` resolves the annotation under
    # ``from __future__ import annotations`` — exactly how real tools are declared.
    ...


def test_auto_schema_enum_and_injected_params_skipped():
    schema = derive_tool_schema(_enum_handler)
    # self / identity / vessel_id / *args / **kwargs are framework-injected -> excluded.
    assert set(schema["properties"]) == {"mode"}
    assert schema["properties"]["mode"] == {"enum": ["fast", "slow"]}


def test_tool_decorator_and_function_spec():
    r = Registry()

    @tool(registry=r, description="explicit desc")
    def ping(host: str) -> bool:
        return True

    assert ping("x") is True  # handler unchanged
    spec = r.tool_function_spec("ping")
    assert spec["type"] == "function"
    assert spec["function"]["name"] == "ping"
    assert spec["function"]["description"] == "explicit desc"
    assert spec["function"]["parameters"]["properties"]["host"] == {"type": "string"}


def test_explicit_schema_overrides_auto():
    r = Registry()
    custom = {"type": "object", "properties": {"x": {"type": "string"}}, "additionalProperties": False}

    def h(y: int) -> None:
        ...

    reg = r.register_tool("with_schema", h, schema=custom)
    assert reg.schema is custom


def test_resolve_tool_by_capability_key():
    r = Registry()

    def h() -> None:
        ...

    r.register_tool("toolname", h, capability="cap-key")
    assert r.resolve_capability("toolname") is not None      # by name
    assert r.resolve_capability("cap-key") is not None        # by capability


# --------------------------------------------------------------------------- #
# Skill discovery (filesystem)
# --------------------------------------------------------------------------- #
_SKILL_SRC = textwrap.dedent(
    '''
    CASE = {
        "name": "demo-skill",
        "capability": "demo-skill",
        "domain": "demo",
        "description": "A demo skill.",
    }

    def run(context=None):
        return {"ok": True, "context": context}
    '''
)


def _make_skill_tree(tmp_path: Path, capability: str = "demo-skill", name: str = "demo_skill") -> Path:
    """Lay out ``<tmp>/<Domain>/skills/<name>.py`` mirroring the generator's layout."""
    skills = tmp_path / f"Domain-{name}" / "skills"
    skills.mkdir(parents=True)
    src = _SKILL_SRC
    if capability != "demo-skill":
        src = src.replace('"capability": "demo-skill"', f'"capability": "{capability}"')
    skill_file = skills / f"{name}.py"
    skill_file.write_text(src, encoding="utf-8")
    return skill_file


def test_register_single_skill(tmp_path):
    r = Registry()
    skill_file = _make_skill_tree(tmp_path)
    reg = r.register_skill(skill_file)
    assert reg.kind is RegistrationKind.SKILL
    assert reg.capability == "demo-skill"
    assert callable(reg.handler)
    assert reg.handler() == {"ok": True, "context": None}


def test_register_skill_without_case_fails(tmp_path):
    bad = tmp_path / "skills"
    bad.mkdir()
    f = bad / "nocase.py"
    f.write_text("X = 1\n", encoding="utf-8")
    with pytest.raises(RegistryError):
        r = Registry()
        r.register_skill(f)


def test_discover_skills_walks_tree(tmp_path):
    _make_skill_tree(tmp_path)
    r = Registry()
    added = r.discover_skills(tmp_path)
    assert len(added) == 1
    # Resolution finds the discovered skill by its capability key.
    resolved = r.resolve_capability("demo-skill")
    assert resolved is not None and resolved.kind is RegistrationKind.SKILL


def test_discover_missing_root_is_fail_soft():
    r = Registry()
    assert r.discover_skills("/no/such/path/at/all") == []


def test_discover_warns_on_case_without_capability(tmp_path, caplog):
    # ACK-2 (#503): a module with a CASE dict but an empty/missing 'capability' is dropped in the bulk
    # scan — now WITH a diagnostic (it used to vanish silently; only single-file register_skill raised).
    sk = tmp_path / "skills"
    sk.mkdir()
    (sk / "broken.py").write_text("CASE = {'name': 'x', 'description': 'd'}\n", encoding="utf-8")  # no capability
    r = Registry()
    with caplog.at_level(logging.WARNING, logger="ack.registry"):
        added = r.discover_skills(tmp_path)
    assert added == []                                    # dropped, as before
    assert "no/empty 'capability'" in caplog.text         # but no longer silent


def test_skill_roots_are_lazily_scanned(tmp_path):
    _make_skill_tree(tmp_path)
    # roots are scanned on first resolve / list_all, not at construction.
    r = Registry(skill_roots=[tmp_path])
    assert r.resolve_capability("demo-skill") is not None


def test_duplicate_skill_capability_is_fail_loud(tmp_path):
    r = Registry()
    f1 = _make_skill_tree(tmp_path)
    # a second file in a different dir with the SAME capability
    other = tmp_path / "Other" / "skills"
    other.mkdir(parents=True)
    f2 = other / "again.py"
    f2.write_text(_SKILL_SRC, encoding="utf-8")
    r.register_skill(f1)
    with pytest.raises(DuplicateRegistrationError):
        r.register_skill(f2)


# --------------------------------------------------------------------------- #
# MCP binding (delegation to the existing vessel-scoped provider)
# --------------------------------------------------------------------------- #
class _FakeMCPProvider:
    def __init__(self):
        self.calls = []

    async def list_tools(self, vessel_id):
        self.calls.append(("list", vessel_id))
        return [{"name": "wf-tool", "vessel": vessel_id}]

    async def get_tool(self, vessel_id, name):
        self.calls.append(("get", vessel_id, name))
        return ({"name": name}, lambda *a, **k: None) if name == "wf-tool" else None


def test_mcp_binding_delegates():
    r = Registry()
    assert r.mcp_bound is False
    provider = _FakeMCPProvider()
    r.bind_mcp_provider(provider)
    assert r.mcp_bound is True

    tools = asyncio.run(r.list_mcp_tools("vessel-a"))
    assert tools == [{"name": "wf-tool", "vessel": "vessel-a"}]

    resolved = asyncio.run(r.resolve_mcp_tool("vessel-a", "wf-tool"))
    assert resolved is not None and resolved[0]["name"] == "wf-tool"
    assert asyncio.run(r.resolve_mcp_tool("vessel-a", "nope")) is None


def test_mcp_unbound_returns_empty():
    r = Registry()
    assert asyncio.run(r.list_mcp_tools("t")) == []
    assert asyncio.run(r.resolve_mcp_tool("t", "x")) is None


def test_real_mcp_tool_registry_satisfies_provider_contract():
    # The kernel binds an EXISTING vessel-scoped provider (KGC-375/404) without
    # modifying it.
    #
    # ADAPTATION: the concrete ``core.mcp.tool_registry.MCPToolRegistry`` provider is
    # NOT part of the standalone ``core/ack`` package (the ack kernel imports stdlib
    # only and only *binds* an injected provider). The original intent — that the
    # registry binds a real, default-deny / fail-closed provider that exposes nothing
    # when its feature is OFF, without raising — is preserved here with a faithful
    # minimal provider that implements the same async ``list_tools`` / ``get_tool``
    # contract and is fail-closed by default.
    class _FailClosedProvider:
        """Mirrors the real provider's default-deny posture: feature OFF -> nothing."""

        def __init__(self, *, enabled: bool = False):
            self._enabled = enabled

        async def list_tools(self, vessel_id: str):
            if not self._enabled:
                return []
            return [{"name": "real-tool", "vessel": vessel_id}]

        async def get_tool(self, vessel_id: str, name: str):
            if not self._enabled:
                return None
            return ({"name": name}, lambda *a, **k: None)

    r = Registry()
    r.bind_mcp_provider(_FailClosedProvider())
    assert r.mcp_bound is True
    # Feature is OFF by default -> provider exposes nothing (fail-closed), no error.
    assert asyncio.run(r.list_mcp_tools("vessel-a")) == []


# --------------------------------------------------------------------------- #
# list_all / summary (doctor overview) + integration
# --------------------------------------------------------------------------- #
def test_list_all_spans_all_kinds(tmp_path):
    _make_skill_tree(tmp_path)
    r = Registry(skill_roots=[tmp_path])

    class TT:
        pass

    def tl() -> None:
        ...

    r.register_task_type("tt", TT)
    r.register_tool("tl", tl)

    regs = r.list_all()
    kinds = {x.kind for x in regs}
    assert RegistrationKind.TASK_TYPE in kinds
    assert RegistrationKind.TOOL in kinds
    assert RegistrationKind.SKILL in kinds  # lazily discovered from skill_roots

    summary = r.summary()
    assert summary["total"] == len(regs)
    assert summary["by_kind"]["skill"] == 1
    assert isinstance(summary["registrations"], list)


def test_resolve_unknown_returns_none():
    assert Registry().resolve_capability("does-not-exist") is None


def test_fresh_registry_list_all_is_empty_list():
    # Validation step 1: ``Registry().list_all()`` returns a list (here empty).
    assert Registry().list_all() == []


def test_default_registry_is_singleton():
    assert get_registry() is get_registry()


def test_spec_reexport_is_same_class():
    assert RegistryFromSpec is Registry


def test_integration_drop_spec_then_discover(tmp_path):
    """Integration: a new skill 'dropped' into a skills/ folder is auto-discovered
    and resolves end-to-end — the core ack-registry promise (no kernel code-edit)."""
    r = Registry(skill_roots=[tmp_path])
    assert r.resolve_capability("late-skill") is None  # nothing there yet

    # Drop a new case AFTER construction.
    r2 = Registry(skill_roots=[tmp_path])
    _make_skill_tree(tmp_path, capability="late-skill", name="late_skill")
    resolved = r2.resolve_capability("late-skill")
    assert resolved is not None and resolved.capability == "late-skill"
