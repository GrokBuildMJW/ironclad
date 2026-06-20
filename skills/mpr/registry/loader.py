"""PanelRegistry — discover / validate / resolve the domain panels (Spec 05 §6).

Rides the *pattern* of ``ack.Registry`` (file-path module loading, lazy scan, two error postures) but
imports nothing private from it: ``_load_module`` is a clean-room copy of the public
``spec_from_file_location`` recipe (registry.py:389-403), not an import of that private static.

Two deliberate, different error postures (Spec 05 §6.2 / §11):
* **Discovery robustness — fail-soft:** an *unloadable/invalid* panel file is skipped + ``logger.warning``
  and the run continues (mirrors ``discover_skills``, registry.py:366).
* **Identity collision — fail-loud:** *two files for the same ``domain``* → ``DuplicatePanelError``
  (no silent keep-first). This is MPR's OWN strictness, NOT inherited from ironclad's bulk
  ``discover_skills`` (which is keep-first, registry.py:372-377); the loud wording mirrors the *single*
  path ``register_skill`` (registry.py:338), because a duplicate domain is a content bug.

Side-effect-free at import: discovery only runs when ``discover()`` / ``get_registry()`` is called.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
import threading
from pathlib import Path
from typing import Optional, Union

from .schema import Panel, validate_panel_json

logger = logging.getLogger(__name__)

#: Sentinel so an explicit ``PANEL = None`` is distinguishable from a missing attribute (LOW-1).
_MISSING = object()


class DuplicatePanelError(RuntimeError):
    """Two panel files declare the same ``domain`` — an identity collision (fail-loud)."""


class PanelRegistry:
    """Holds ``domain -> Panel`` plus the source path of each (for the loud dupe error)."""

    def __init__(self) -> None:
        self._panels: dict[str, Panel] = {}     # domain -> Panel
        self._source: dict[str, str] = {}       # domain -> file path (for the error text)
        self._lock = threading.Lock()           # mirrors ack.Registry's self._lock (registry.py:335)

    # -- registration -------------------------------------------------------
    def register(self, panel: Panel, *, source: str = "") -> None:
        """Add a (already-validated) panel. FAIL-LOUD on a domain dupe from a *different* source.

        Re-registering the same domain from the same source is idempotent (a re-``discover`` of the
        same file must not raise). The check+set is locked so the dupe guarantee holds under
        concurrent discovery (the ack pattern this rides locks the same way).
        """
        with self._lock:
            existing = self._source.get(panel.domain)
            if panel.domain in self._panels and existing != source:
                raise DuplicatePanelError(
                    f"panel domain {panel.domain!r} already registered (by {existing})"
                )
            self._panels[panel.domain] = panel
            self._source[panel.domain] = source

    # -- discovery ----------------------------------------------------------
    def discover(self, root: Union[str, Path]) -> list[Panel]:
        """Walk the panels dir for ``*.py``, pull each module's ``PANEL``, validate + register.

        ``root`` may be the plugin root (then ``root/panels`` is scanned) or the panels dir itself —
        whichever exists. Missing root → ``[]`` (fail-soft, like ``discover_skills`` registry.py:355).
        A broken/invalid panel file is skipped + warned (fail-soft); a duplicate ``domain`` is
        fail-loud (``DuplicatePanelError`` escapes — it is NOT swallowed by the per-file guard).
        """
        base = Path(root)
        panels_dir = base / "panels" if (base / "panels").is_dir() else base
        if not panels_dir.is_dir():
            return []
        added: list[Panel] = []
        for py in sorted(panels_dir.glob("*.py")):
            if py.stem.startswith("_"):
                continue
            if not py.is_file():
                continue  # a dir/symlink named '*.py' — skip cleanly (no cryptic load error)
            try:
                panel = self._panel_from_file(py)
            except Exception as exc:  # noqa: BLE001 — a broken panel must not abort discovery
                logger.warning("mpr-registry: skipping unloadable panel %s: %s", py, exc)
                continue
            if panel is None:
                continue  # a .py without a PANEL attribute (not a panel file)
            # register() is OUTSIDE the guard → a domain dupe is fail-loud (escapes discovery).
            self.register(panel, source=str(py))
            added.append(panel)
        return added

    def _panel_from_file(self, path: Path) -> Optional[Panel]:
        mod = self._load_module(path)
        obj = getattr(mod, "PANEL", _MISSING)
        if obj is _MISSING:
            return None  # a .py without a PANEL attribute (not a panel file) — silent skip
        if isinstance(obj, Panel):
            return obj
        if isinstance(obj, dict):
            return validate_panel_json(obj)  # ValidationError → caught fail-soft by discover()
        # explicit PANEL=None or any other junk → loud TypeError → fail-soft warn (not silent).
        raise TypeError(f"PANEL in {path} is not a Panel or dict (got {type(obj).__name__})")

    @staticmethod
    def _load_module(path: Path):
        """Load a panel ``.py`` by file path — clean-room copy of registry.py:389-403's recipe.

        Bypasses package ``__init__`` and keeps discovery independent of ``sys.path``; gives each file
        a unique synthetic module name so two same-named files in different dirs never collide.
        """
        unique = f"mpr_panel_{abs(hash(str(path.resolve())))}_{path.stem}"
        spec = importlib.util.spec_from_file_location(unique, path)
        if spec is None or spec.loader is None:  # pragma: no cover - defensive
            raise RuntimeError(f"cannot load panel module {path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[unique] = mod
        spec.loader.exec_module(mod)
        return mod

    # -- lookup -------------------------------------------------------------
    def resolve(self, domain: str) -> Optional[Panel]:
        return self._panels.get(domain)

    def domains(self) -> list[str]:
        return sorted(self._panels)

    def versions(self) -> dict[str, int]:
        """``domain -> Panel.version`` for every registered panel (audit/replay, Spec 05 §9).

        The run-manifest (Spec 07) records the ``panel_version`` a run executed against, so a replay
        knows which panel fassung it ran with. Bumping ``Panel.version`` on a content change is the
        registry's versioning contract; adding a new domain is just a new file (discover picks it up).
        """
        return {dom: panel.version for dom, panel in self._panels.items()}


# ── Process-wide lazy singleton (Spec 05 §6.3) ────────────────────────────────────────────────────
_REGISTRY: Optional[PanelRegistry] = None
_REGISTRY_LOCK = threading.Lock()


def get_registry(root: Union[str, Path, None] = None, *, rediscover: bool = False) -> PanelRegistry:
    """Lazily build + discover the process-wide PanelRegistry on first call (boot sequence §6.3).

    Built on demand (never at import → side-effect-free). ``rediscover=True`` forces a fresh build
    (used by tests / a config reload); otherwise the first-built registry is reused. The build is
    locked so a concurrent first call cannot race the check-then-set on ``_REGISTRY``.
    """
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None or rediscover:
            reg = PanelRegistry()
            if root is not None:
                reg.discover(root)
            _REGISTRY = reg
        return _REGISTRY
