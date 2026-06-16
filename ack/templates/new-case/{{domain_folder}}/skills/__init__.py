"""Self-discovering Skill registry for the {{domain_title}} domain.

Registration stub (ACK generator). Cases are DISCOVERED, not hardcoded: every
sibling ``*.py`` module that exposes a ``CASE`` dict is collected here. A new case
registers itself by merely being scaffolded next to this file — no import edit, no
entry-point change.

    from <pkg> import REGISTRY, get_case
    REGISTRY["{{capability_key}}"]   # -> the CASE dict
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

DOMAIN = "{{domain_name}}"

REGISTRY: dict[str, dict] = {}


def _discover() -> None:
    REGISTRY.clear()
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        mod: ModuleType = importlib.import_module(f"{__name__}.{info.name}")
        case = getattr(mod, "CASE", None)
        if isinstance(case, dict) and case.get("capability"):
            REGISTRY[case["capability"]] = {**case, "_module": mod.__name__, "run": getattr(mod, "run", None)}


def get_case(capability: str) -> dict | None:
    if not REGISTRY:
        _discover()
    return REGISTRY.get(capability)


def all_cases() -> dict[str, dict]:
    if not REGISTRY:
        _discover()
    return dict(REGISTRY)


_discover()
