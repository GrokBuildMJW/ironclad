"""Project overlay discipline (ADR-0011, AD-1 / S4) — bound what a per-project config overlay may change
and keep its paths inside the project root.

Two fail-closed guards the quiesced switch (S5) applies when it builds a project's effective config:

1. ``apply_project_overlay(base, overlay)`` — deep-merge a project's overlay onto the base config, but
   DROP any overlay key under a LOCKED prefix. The locked set protects the deployment-wide, security- and
   isolation-bearing config a project must never re-point: ``connection`` (where the model/memory live),
   ``security`` (trust profile / sealing), ``setup`` (boot operating mode), ``search`` (web-search gate),
   ``generation`` (reply-language / decoding settings), ``plugins_dir`` (3rd-party load root), and
   ``providers.budget`` (the cost ceiling). An optional CLOSED ``allow`` set further restricts overrides to
   named top-level sections (deny-by-default); the default is open-except-locked, and S5 / a deployment may
   pass a closed allowlist once the overridable surface is finalized. Returns ``(merged, dropped)`` so the
   switch can log what a project tried to lock-override.

2. ``contain(project_root, candidate)`` — canonicalize *candidate* (resolving symlinks + ``..``) and assert
   it stays INSIDE *project_root*, closing the verbatim-absolute / traversal / symlink override of
   ``state_root``/``vault_root``/``session``/``logs``/``memory-cache``. Raises ``PathEscape`` on escape.

Pure, stdlib-only, secret-free; imports nothing from the engine.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Iterable, Optional, Tuple

#: Dotted config prefixes a project overlay may NEVER set (deployment-wide / security / isolation-bearing).
LOCKED_PREFIXES: Tuple[str, ...] = (
    "connection",          # where the model + cold memory live (LAN/host) — never per-project
    "security",            # trust profile / sealing / egress
    "setup",               # boot-fixed operating mode (server|local)
    "search",              # web-search adapter + trust gate
    "generation",          # reply-language + decoding settings — never per-project
    "plugins_dir",         # 3rd-party plugin load root
    "providers.budget",    # the cost ceiling (a project must not raise its own budget)
)


class PathEscape(ValueError):
    """A candidate path resolved outside the project root (traversal / absolute / symlink escape)."""


def _is_locked(dotted: str, locked: Tuple[str, ...]) -> bool:
    """*dotted* IS a locked path (the prefix itself or a descendant of it)."""
    return any(dotted == p or dotted.startswith(p + ".") for p in locked)


def _is_locked_ancestor(dotted: str, locked: Tuple[str, ...]) -> bool:
    """*dotted* is an ANCESTOR of a locked path (so replacing it with a non-dict would drop/forge a
    locked descendant — e.g. ``providers`` is an ancestor of ``providers.budget``)."""
    return any(p.startswith(dotted + ".") for p in locked)


def _is_allowed(dotted: str, allow: set) -> bool:
    return dotted.split(".", 1)[0] in allow


def apply_project_overlay(base: dict, overlay: dict, *,
                          locked: Iterable[str] = LOCKED_PREFIXES,
                          allow: "Optional[Iterable[str]]" = None) -> "Tuple[dict, list]":
    """Return ``(merged, dropped)``: *overlay* deep-merged onto *base*, with any key under a LOCKED prefix
    DROPPED — including the parent-replacement bypasses (an overlay dict that would FORGE a locked
    descendant where base has none, or a non-dict that would DROP a base's locked descendant by replacing
    its ancestor). When *allow* is a closed set, keys outside an allowed top-level section are dropped too.
    *base* is not mutated. ``dropped`` is the sorted list of rejected dotted keys (for the switch to log)."""
    dropped: list = []
    merged = copy.deepcopy(base)
    locked = tuple(locked)                                # materialize: a one-shot generator would unlock later keys
    allow_set = set(allow) if allow is not None else None

    def _walk(b: dict, o: dict, path: str) -> None:
        for k, v in o.items():
            dotted = f"{path}.{k}" if path else k
            if _is_locked(dotted, locked) or (allow_set is not None and not _is_allowed(dotted, allow_set)):
                dropped.append(dotted)
                continue
            if isinstance(v, dict):
                # ALWAYS recurse into an overlay dict (even if base has no dict here) so a locked
                # descendant inside it is dropped, never forged.
                had_dict = isinstance(b.get(k), dict)
                target = b[k] if had_dict else {}
                _walk(target, v, dotted)
                if had_dict:
                    b[k] = target
                elif target:                             # overlay introduced only non-locked keys → attach
                    b[k] = target
                # else: the overlay subtree was entirely locked → introduce nothing (no empty forging)
            elif _is_locked_ancestor(dotted, locked):
                # a non-dict overlay at an ancestor of a locked path would drop/forge that locked
                # descendant — reject it (base's locked subtree stays).
                dropped.append(dotted)
            else:
                b[k] = copy.deepcopy(v)

    if isinstance(base, dict) and isinstance(overlay, dict):
        _walk(merged, overlay, "")
    return merged, sorted(dropped)


def contain(project_root: "Path | str", candidate: "Path | str") -> Path:
    """Resolve *candidate* (relative → under *project_root*; symlinks + ``..`` canonicalized) and return it
    ONLY if it stays inside the resolved *project_root*; else raise ``PathEscape``. The single chokepoint
    that forces a project's state_root/vault_root/session/logs/memory-cache to live under its own root."""
    root = Path(project_root).resolve()
    cand = Path(candidate)
    full = cand if cand.is_absolute() else (root / cand)
    resolved = full.resolve()
    if resolved != root and root not in resolved.parents:
        raise PathEscape(f"path escapes the project root: {resolved} not within {root}")
    return resolved
