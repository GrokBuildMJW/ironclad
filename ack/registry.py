"""Registry / Discovery — Agent-Contract-Kernel component 4.

> **Discovery, not hardcoding.** A new case = drop a Spec + register (automatically);
> never a code-edit of the kernel.

Four discovery mechanisms, one resolution surface (mirrors the design SSOT,
component 4):

  1. **Task-types** — in-process classes, registered by a ``@task_type`` decorator
     (or :meth:`Registry.register_task_type`). Drives routing.
  2. **Tools** — LLM/function tools, registered by a ``@tool`` decorator that
     *auto-derives* a (grammar-clean) JSON-Schema from the handler signature, or
     :meth:`Registry.register_tool` with an explicit schema.
  3. **MCP tools** — cross-process, vessel-scoped tools provided by an EXTERNAL
     MCP tool-registry provider. The kernel registry *binds* that provider (it does
     not re-implement it) so resolution can route to MCP without duplicating the
     provider's Zero-Trust / default-deny logic. The provider is injected, optional.
  4. **Skills** — procedural ``skills/*.py`` modules exposing a ``CASE`` descriptor
     (``{name, capability, domain, description}`` + optional ``run``), discovered
     from a filesystem root. This is exactly the contract the generator emits and
     the doctor validates.

**Standalone by design.** This module imports stdlib only — no app/runtime deps at
module load — so it is importable from the host doctor or a bare test runner. The MCP
provider is *injected* (lazy, optional), keeping the same posture as ``case_spec.py``
so the doctor can load it by file path.

**Grammar-safe auto-schema.** Derived tool schemas never carry the XGrammar-V1 array
keywords (``minItems``/``maxItems``/``uniqueItems``/``contains`` …) that 400 a vLLM
``structured_outputs`` request — cardinality stays a validator concern, exactly as
``ack.case_spec`` establishes.
"""
from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import pkgutil
import sys
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import ModuleType, UnionType
from typing import Any, Callable, Optional, Union, get_args, get_origin, get_type_hints

logger = logging.getLogger(__name__)

__all__ = [
    "RegistrationKind",
    "Registration",
    "Registry",
    "DuplicateRegistrationError",
    "RegistryError",
    "get_registry",
    "task_type",
    "tool",
    "derive_tool_schema",
]


class RegistryError(RuntimeError):
    """Base class for registry failures (always carries an actionable message)."""


class DuplicateRegistrationError(RegistryError):
    """A capability/name is already claimed — collisions are fail-loud, never silent.

    Silent last-writer-wins is exactly the drift the doctor (KGC-553, checks 3 & 6)
    exists to catch; the live registry refuses the collision at registration time.
    """


class RegistrationKind(str, Enum):
    """What a registered capability *is* — drives how :class:`Registration` is used."""

    TASK_TYPE = "task_type"
    TOOL = "tool"
    SKILL = "skill"
    MCP = "mcp"


@dataclass(frozen=True)
class Registration:
    """One resolved entry in the registry (uniform across all four kinds).

    ``capability`` is the resolution key (the same drift-free key the gap-tracking
    MAPPING / TaskStore ``capability`` field use). Exactly one of ``cls`` / ``handler``
    is set for task-types / tools; skills carry their ``case`` dict (and ``handler`` =
    the optional ``run`` callable). ``source`` is a human/CI-readable origin (module
    name or file path) for the doctor's ``list_all`` overview.
    """

    kind: RegistrationKind
    name: str
    capability: str
    description: str = ""
    source: str = ""
    cls: Optional[type] = None
    handler: Optional[Callable[..., Any]] = None
    schema: Optional[dict[str, Any]] = None
    case: Optional[dict[str, Any]] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        """Flat, JSON-friendly view (for ``--json`` doctor output / logging)."""
        return {
            "kind": self.kind.value,
            "name": self.name,
            "capability": self.capability,
            "description": self.description,
            "source": self.source,
            "has_handler": self.handler is not None,
            "has_schema": self.schema is not None,
        }


# --------------------------------------------------------------------------- #
# Auto-schema — derive a grammar-clean JSON-Schema from a handler signature
# --------------------------------------------------------------------------- #

#: Minimal Python -> JSON-Schema type map. Anything unrecognised falls back to a
#: bare ``string`` (the safest grammar-friendly default).
_JSON_PRIMITIVES: dict[type, str] = {
    str: "string",
    bool: "boolean",
    int: "integer",
    float: "number",
}


def _annotation_to_schema(annotation: Any) -> dict[str, Any]:
    """Map a type annotation to its JSON-Schema value type.

    Describes ONLY the accepted VALUE type — never call-site requiredness, which is derived exclusively
    from the parameter's default (#1535). The produced schema never uses array-cardinality keywords so it
    stays XGrammar-clean by construction.
    """
    if annotation is inspect.Parameter.empty or annotation is Any:
        return {"type": "string"}

    origin = get_origin(annotation)

    # Optional[X] / Union[..., None] / PEP-604 `X | None` (get_origin is types.UnionType, NOT typing.Union).
    # #1535: an Optional annotation controls the accepted VALUE (it admits null) — it must NOT be conflated
    # with requiredness. The null arm is preserved (matching pydantic's own Optional rendering `{"anyOf":
    # [<T>, {"type": "null"}]}`, which is grammar-clean — the XGrammar lint allows anyOf), so passing None
    # stays schema-valid. Requiredness is decided by the caller from param.default.
    if origin is Union or origin is UnionType:
        all_args = get_args(annotation)
        args = [a for a in all_args if a is not type(None)]  # noqa: E721
        admits_none = len(args) != len(all_args)
        if len(args) == 1:
            inner = _annotation_to_schema(args[0])
            return {"anyOf": [inner, {"type": "null"}]} if admits_none else inner
        # Heterogeneous or None-only union -> permissive (untyped); {} already admits null.
        return {}

    if origin in (list, tuple, set, frozenset):
        item_args = get_args(annotation)
        item_schema = _annotation_to_schema(item_args[0]) if item_args else {}
        # NOTE: no minItems/maxItems/uniqueItems — those 400 under XGrammar V1.
        return {"type": "array", "items": item_schema or {"type": "string"}}

    if origin is dict:
        return {"type": "object"}

    if isinstance(annotation, type):
        if annotation in _JSON_PRIMITIVES:
            return {"type": _JSON_PRIMITIVES[annotation]}
        if issubclass(annotation, Enum):
            return {"enum": [e.value for e in annotation]}
        # A pydantic-ish / dataclass object -> opaque object (kept grammar-safe).
        return {"type": "object"}

    return {"type": "string"}


#: Parameters never exposed in a tool schema (framework-injected, not model-supplied).
_SKIP_PARAMS = {"self", "cls", "context", "identity", "vessel_id", "_"}


def derive_tool_schema(handler: Callable[..., Any]) -> dict[str, Any]:
    """Derive a closed, grammar-clean JSON-Schema for *handler*'s parameters.

    - ``additionalProperties: false`` + an explicit ``required`` list → XGrammar can
      fully close the object (the model cannot omit a required field or invent keys),
      matching the ``ack-case-spec`` hard-floor posture.
    - Framework-injected params (``self``/``context``/``identity``/``vessel_id`` …) and
      ``*args`` / ``**kwargs`` are excluded — they are not model-supplied arguments.
    - A param with a default is optional; one without is required.
    """
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):  # builtins / C-callables without a signature
        return {"type": "object", "properties": {}, "additionalProperties": False}

    # Prefer resolved type hints (handles ``from __future__ import annotations``).
    try:
        hints = get_type_hints(handler)
    except Exception:  # noqa: BLE001 — unresolved forward refs must not break discovery
        hints = {}

    properties: dict[str, Any] = {}
    required: list[str] = []
    for pname, param in sig.parameters.items():
        if pname in _SKIP_PARAMS:
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        annotation = hints.get(pname, param.annotation)
        properties[pname] = _annotation_to_schema(annotation)
        # #1535: requiredness follows the SIGNATURE, not the annotation's nullability — a param with no
        # default is required even when typed Optional[T]/`T | None` (the null arm just makes None a valid
        # VALUE). Deriving it from the annotation let a required `limit: int | None` be omitted, so the
        # model could emit {} and dispatch would crash with a missing-argument TypeError.
        if param.default is inspect.Parameter.empty:
            required.append(pname)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


# --------------------------------------------------------------------------- #
# The Registry
# --------------------------------------------------------------------------- #


class Registry:
    """Discover and resolve task-types, tools, skills and (bound) MCP tools.

    Thread-safe (a single lock guards the maps); construction is side-effect-free —
    nothing is discovered until you register / discover explicitly. A process-wide
    default instance is available via :func:`get_registry` (the target of the
    ``@task_type`` / ``@tool`` decorators).
    """

    def __init__(self, *, skill_roots: Optional[list[Union[str, Path]]] = None) -> None:
        self._lock = threading.RLock()
        self._task_types: dict[str, Registration] = {}
        self._tools: dict[str, Registration] = {}
        self._skills: dict[str, Registration] = {}
        self._mcp_provider: Any = None
        # Default roots to scan for ``skills/`` packages (lazily, on first resolve).
        # Absent roots are silently skipped (fail-soft) — in-container the vault is
        # not mounted, so this is empty there; host/doctor pass explicit roots.
        self._skill_roots: list[Path] = [Path(r) for r in (skill_roots or [])]
        self._skills_scanned = False

    # -- task-types ----------------------------------------------------------

    def register_task_type(self, name: str, cls: type, *, capability: Optional[str] = None,
                           description: str = "") -> Registration:
        """Register an in-process task-type class under *name* (fail-loud on collision)."""
        cap = capability or name
        reg = Registration(
            kind=RegistrationKind.TASK_TYPE,
            name=name,
            capability=cap,
            description=description or (inspect.getdoc(cls) or "").split("\n", 1)[0],
            source=f"{cls.__module__}.{cls.__qualname__}",
            cls=cls,
        )
        with self._lock:
            if name in self._task_types:
                raise DuplicateRegistrationError(
                    f"task_type {name!r} already registered "
                    f"(by {self._task_types[name].source})"
                )
            self._task_types[name] = reg
        logger.debug("registered task_type %s -> %s", name, reg.source)
        return reg

    # -- tools ---------------------------------------------------------------

    def register_tool(self, name: str, handler: Callable[..., Any], *,
                      schema: Optional[dict[str, Any]] = None,
                      capability: Optional[str] = None,
                      description: str = "") -> Registration:
        """Register an LLM/function *tool*. Schema is auto-derived if not supplied.

        The derived schema is grammar-clean (no XGrammar-unsupported keywords), so a
        registered tool is directly usable as a vLLM ``tool_choice`` target.
        """
        cap = capability or name
        reg = Registration(
            kind=RegistrationKind.TOOL,
            name=name,
            capability=cap,
            description=description or (inspect.getdoc(handler) or "").split("\n", 1)[0],
            source=f"{getattr(handler, '__module__', '?')}.{getattr(handler, '__qualname__', name)}",
            handler=handler,
            schema=schema if schema is not None else derive_tool_schema(handler),
        )
        with self._lock:
            if name in self._tools:
                raise DuplicateRegistrationError(
                    f"tool {name!r} already registered (by {self._tools[name].source})"
                )
            self._tools[name] = reg
        logger.debug("registered tool %s -> %s", name, reg.source)
        return reg

    def tool_function_spec(self, name: str) -> dict[str, Any]:
        """Return the OpenAI/vLLM ``{"type":"function","function":{…}}`` spec for a tool."""
        reg = self._tools.get(name)
        if reg is None:
            raise RegistryError(f"unknown tool: {name!r}")
        return {
            "type": "function",
            "function": {
                "name": reg.name,
                "description": reg.description,
                "parameters": reg.schema or {"type": "object", "properties": {}},
            },
        }

    # -- skills --------------------------------------------------------------

    def register_skill(self, path: Union[str, Path]) -> Registration:
        """Discover + register a single skill module from a ``.py`` *path*.

        The module must expose a ``CASE`` dict with a ``capability`` (the generator's
        contract). ``run`` (if present) becomes the registration's handler.
        """
        p = Path(path)
        if not p.is_file():
            raise RegistryError(f"skill file not found: {p}")
        mod = self._load_skill_module(p)
        reg = self._registration_from_skill_module(mod, source=str(p))
        if reg is None:
            raise RegistryError(
                f"{p} exposes no CASE dict with a 'capability' "
                f"(expected CASE = {{'capability': '<key>', ...}})"
            )
        with self._lock:
            existing = self._skills.get(reg.capability)
            if existing is not None and existing.source != reg.source:
                raise DuplicateRegistrationError(
                    f"skill capability {reg.capability!r} already registered "
                    f"(by {existing.source})"
                )
            self._skills[reg.capability] = reg
        logger.debug("registered skill %s -> %s", reg.capability, reg.source)
        return reg

    def discover_skills(self, root: Union[str, Path]) -> list[Registration]:
        """Walk *root* for ``skills/`` packages and register every ``CASE`` found.

        Generalises the doctor's Check-6 scan and the generator's per-domain
        ``skills/__init__.py`` onto the kernel. Missing root → ``[]`` (fail-soft).
        Returns the registrations added by this call.
        """
        base = Path(root)
        if not base.is_dir():
            return []
        added: list[Registration] = []
        for skills_dir in sorted(base.glob("**/skills")):
            if not skills_dir.is_dir():
                continue
            for py in sorted(skills_dir.glob("*.py")):
                if py.stem.startswith("_"):
                    continue
                try:
                    mod = self._load_skill_module(py)
                    reg = self._registration_from_skill_module(mod, source=str(py))
                except Exception as exc:  # noqa: BLE001 — a broken skill must not abort discovery
                    logger.warning("registry: skipping unloadable skill %s: %s", py, exc)
                    continue
                if reg is None:
                    # ACK-2 (#503): a module with a CASE dict but an empty/typo'd 'capability' was dropped
                    # with ZERO diagnostics in the bulk scan (single-file register_skill raises clearly).
                    if isinstance(getattr(mod, "CASE", None), dict):
                        logger.warning("registry: skill %s has a CASE but no/empty 'capability' — skipped", py)
                    continue
                with self._lock:
                    existing = self._skills.get(reg.capability)
                    if existing is not None and existing.source != reg.source:
                        logger.warning(
                            "registry: duplicate skill capability %r (%s vs %s) — keeping first",
                            reg.capability, existing.source, reg.source)
                        continue
                    self._skills[reg.capability] = reg
                added.append(reg)
        return added

    @staticmethod
    def discover_playbooks(root: Union[str, Path]) -> list:
        """Discover ``SKILL.md`` playbook skills under *root* (the second skill kind, ADR-0001).

        Sits alongside :meth:`discover_skills` (typed ``.py`` tools). Returns a list of
        :class:`ack.playbook.Playbook` (metadata eager, body/references lazy). Fail-soft:
        a broken package is skipped, missing root → ``[]``.
        """
        from ack.playbook import discover_playbooks as _discover
        return _discover(root)

    def _ensure_skills_scanned(self) -> None:
        # ACK-3 (#503): the scan flag was read/written OUTSIDE the lock the registry advertises. Use
        # double-checked locking (the RLock is re-entrant, so discover_skills' per-registration locking
        # nested below is fine) so a concurrent first-touch can't double-scan or race the flag.
        if self._skills_scanned:
            return
        with self._lock:
            if self._skills_scanned:
                return
            for root in self._skill_roots:
                self.discover_skills(root)
            self._skills_scanned = True

    @staticmethod
    def _load_skill_module(path: Path) -> ModuleType:
        """Load a skill ``.py`` by file path (bypasses package ``__init__``).

        Same loader the generator/doctor tests use — keeps discovery independent of
        ``sys.path`` and of any heavy package ``__init__``.
        """
        unique = f"ack_skill_{abs(hash(str(path.resolve())))}_{path.stem}"
        spec = importlib.util.spec_from_file_location(unique, path)
        if spec is None or spec.loader is None:  # pragma: no cover - defensive
            raise RegistryError(f"cannot load skill module {path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[unique] = mod
        spec.loader.exec_module(mod)
        return mod

    @staticmethod
    def _registration_from_skill_module(mod: ModuleType, *, source: str) -> Optional[Registration]:
        case = getattr(mod, "CASE", None)
        if not isinstance(case, dict) or not case.get("capability"):
            return None
        cap = str(case["capability"])
        return Registration(
            kind=RegistrationKind.SKILL,
            name=str(case.get("name") or cap),
            capability=cap,
            description=str(case.get("description") or ""),
            source=source,
            handler=getattr(mod, "run", None),
            case=dict(case),
            metadata={"domain": case.get("domain")},
        )

    # -- MCP binding ---------------------------------------------------------

    def bind_mcp_provider(self, provider: Any) -> None:
        """Bind the EXISTING vessel-scoped MCP tool provider (``MCPToolRegistry``).

        The kernel registry does not re-implement MCP discovery (default-deny,
        Zero-Trust, TOCTOU-safe — KGC-375/404); it delegates to the provider so MCP
        tools share the single resolution surface. The provider's ``list_tools`` /
        ``get_tool`` are vessel-scoped and async, hence the dedicated async methods.
        """
        self._mcp_provider = provider

    @property
    def mcp_bound(self) -> bool:
        return self._mcp_provider is not None

    async def list_mcp_tools(self, vessel_id: str) -> list[Any]:
        """List the MCP tools the bound provider exposes for *vessel_id* (or ``[]``)."""
        if self._mcp_provider is None:
            return []
        return await self._mcp_provider.list_tools(vessel_id)

    async def resolve_mcp_tool(self, vessel_id: str, name: str) -> Optional[Any]:
        """Resolve an MCP tool ``name`` for *vessel_id* via the bound provider."""
        if self._mcp_provider is None:
            return None
        return await self._mcp_provider.get_tool(vessel_id, name)

    # -- resolution ----------------------------------------------------------

    def resolve_capability(self, key: str) -> Optional[Registration]:
        """Resolve a capability/name *key* to its :class:`Registration`, or ``None``.

        Resolution order: task-types, then tools (by name *or* capability), then
        discovered skills (by capability *or* name). MCP tools are vessel-scoped and
        therefore resolved via :meth:`resolve_mcp_tool`, not here.
        """
        with self._lock:
            if key in self._task_types:
                return self._task_types[key]
            if key in self._tools:
                return self._tools[key]
            for reg in self._tools.values():
                if reg.capability == key:
                    return reg
        self._ensure_skills_scanned()
        with self._lock:
            if key in self._skills:
                return self._skills[key]
            for reg in self._skills.values():
                if reg.name == key:
                    return reg
        return None

    def list_all(self) -> list[Registration]:
        """Every registration (task-types + tools + skills), sorted, for the doctor.

        Triggers lazy skill discovery (from the configured roots) so the overview is
        complete. MCP presence is reported separately via :attr:`mcp_bound`.
        """
        self._ensure_skills_scanned()
        with self._lock:
            out = [
                *self._task_types.values(),
                *self._tools.values(),
                *self._skills.values(),
            ]
        return sorted(out, key=lambda r: (r.kind.value, r.capability))

    def summary(self) -> dict[str, Any]:
        """Counts + a flat listing — the shape the doctor / CLI print."""
        regs = self.list_all()
        by_kind: dict[str, int] = {}
        for r in regs:
            by_kind[r.kind.value] = by_kind.get(r.kind.value, 0) + 1
        return {
            "total": len(regs),
            "by_kind": by_kind,
            "mcp_bound": self.mcp_bound,
            "registrations": [r.summary() for r in regs],
        }


# --------------------------------------------------------------------------- #
# Process-wide default registry + decorators
# --------------------------------------------------------------------------- #

_default_registry: Optional[Registry] = None
_default_lock = threading.Lock()


def get_registry() -> Registry:
    """Return the process-wide default registry (decorator target). Lazy singleton."""
    global _default_registry
    if _default_registry is None:
        with _default_lock:
            if _default_registry is None:
                _default_registry = Registry()
    return _default_registry


def task_type(name: Optional[str] = None, *, capability: Optional[str] = None,
              description: str = "", registry: Optional[Registry] = None) -> Callable[[type], type]:
    """Class decorator: register a task-type by *name* (default: the class name).

        @task_type("invoice-chase")
        class InvoiceChase: ...
    """
    def deco(cls: type) -> type:
        reg = registry or get_registry()
        reg.register_task_type(name or cls.__name__, cls, capability=capability, description=description)
        return cls
    return deco


def tool(name: Optional[str] = None, *, schema: Optional[dict[str, Any]] = None,
         capability: Optional[str] = None, description: str = "",
         registry: Optional[Registry] = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Function decorator: register a tool with an auto-derived (or explicit) schema.

        @tool(name="lookup_customer")
        def lookup_customer(customer_id: str, include_orders: bool = False) -> dict: ...

    The handler is returned unchanged (the decorator is registration-only), so the
    function stays directly callable.
    """
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        reg = registry or get_registry()
        reg.register_tool(name or fn.__name__, fn, schema=schema,
                          capability=capability, description=description)
        return fn
    return deco
