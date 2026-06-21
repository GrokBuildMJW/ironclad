"""Playbook skill kind — ``SKILL.md`` packages with progressive disclosure (ADR-0001, #89).

The second skill kind alongside the typed ``CASE``+``run`` tool (``registry.discover_skills``).
A **playbook** is a directory:

    <skill>/
      SKILL.md          # frontmatter (metadata) + markdown body (routing/instructions)
      references/       # docs loaded lazily, only when asked for
      scripts/          # optional file-first helpers (a `check` entry = the validation gate)

Progressive disclosure: the frontmatter ``meta`` is parsed eagerly (cheap); the ``body`` and
each ``reference(name)`` are read **only on access**. Zero external dependencies (stdlib only)
so the kernel stays standalone + secret-free; the frontmatter is a small, strict YAML subset
(flat ``key: value`` scalars + inline ``[a, b]`` lists) — enough for the shared metadata schema
in ``docs/skill-packaging.md``.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

KIND = "playbook"
_REQUIRED = ("capability", "kind", "description")
_LIST_FIELDS = ("trigger", "not_for")
_SCALAR_FIELDS = ("capability", "name", "description", "kind", "type", "domain",
                  "version", "provenance")


class PlaybookError(Exception):
    """A SKILL.md package could not be parsed or failed schema validation."""


def _coerce_scalar(raw: str) -> Any:
    s = raw.strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    for cast in (int, float):
        try:
            return cast(s)
        except ValueError:
            pass
    return s


def _parse_inline_list(raw: str) -> list[str]:
    inner = raw.strip()[1:-1].strip()
    if not inner:
        return []
    return [str(_coerce_scalar(part)) for part in inner.split(",") if part.strip()]


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a SKILL.md into (metadata dict, markdown body).

    Frontmatter is the block between a leading ``---`` and the next ``---``. Supports flat
    ``key: scalar`` and ``key: [a, b]`` inline lists. No frontmatter → ({}, full text).
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict[str, Any] = {}
    body_start = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            body_start = i + 1
            break
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise PlaybookError(f"frontmatter line without ':' → {line!r}")
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if val.startswith("[") and val.endswith("]"):
            meta[key] = _parse_inline_list(val)
        else:
            meta[key] = _coerce_scalar(val)
    if body_start is None:
        raise PlaybookError("frontmatter opened with '---' but never closed")
    return meta, "\n".join(lines[body_start:]).lstrip("\n")


def validate_meta(meta: dict[str, Any]) -> list[str]:
    """Return a list of schema violations ([] = valid). Mirrors docs/skill-packaging.md."""
    errs: list[str] = []
    for req in _REQUIRED:
        if not meta.get(req):
            errs.append(f"missing required field: {req}")
    if meta.get("kind") not in (None, KIND):
        errs.append(f"kind must be {KIND!r}, got {meta.get('kind')!r}")
    for f in _LIST_FIELDS:
        if f in meta and not isinstance(meta[f], list):
            errs.append(f"field {f!r} must be a list")
    for f in ("capability", "description"):
        if f in meta and meta.get(f) is not None and not isinstance(meta[f], str):
            errs.append(f"field {f!r} must be a string")
    return errs


class Playbook:
    """A discovered playbook skill. ``meta`` is eager; ``body``/``reference`` are lazy."""

    def __init__(self, skill_md: Path, meta: dict[str, Any], _raw_body: str) -> None:
        self.path = skill_md.resolve()
        self.dir = self.path.parent
        self.meta = meta
        self._raw_body = _raw_body
        self._body_cache: Optional[str] = None

    @property
    def capability(self) -> str:
        return str(self.meta["capability"])

    @property
    def name(self) -> str:
        return str(self.meta.get("name") or self.capability)

    @property
    def description(self) -> str:
        return str(self.meta.get("description") or "")

    @property
    def body(self) -> str:
        """The markdown body (lazy; cached on first access)."""
        if self._body_cache is None:
            self._body_cache = self._raw_body
        return self._body_cache

    def references(self) -> list[str]:
        """Names of available reference docs (no content read)."""
        refs = self.dir / "references"
        if not refs.is_dir():
            return []
        return sorted(p.name for p in refs.glob("*") if p.is_file())

    def reference(self, name: str) -> str:
        """Read one reference doc by name (lazy). Raises PlaybookError if absent/outside."""
        safe = Path(name).name  # no traversal
        target = self.dir / "references" / safe
        if not target.is_file():
            raise PlaybookError(f"no such reference {safe!r} in {self.capability!r}")
        return target.read_text(encoding="utf-8")

    def matches(self, query: str) -> bool:
        """Trigger routing: any trigger keyword (case-insensitive substring) in the query."""
        q = (query or "").lower()
        return any(str(t).lower() in q for t in self.meta.get("trigger", []) if str(t).strip())

    def metadata(self) -> dict[str, Any]:
        """The cheap, disclosure-first view (no body/references)."""
        return {
            "capability": self.capability, "name": self.name, "kind": KIND,
            "description": self.description, "type": self.meta.get("type"),
            "domain": self.meta.get("domain"), "trigger": self.meta.get("trigger", []),
            "version": self.meta.get("version"), "provenance": self.meta.get("provenance"),
        }


def parse_playbook(skill_md: Path) -> Playbook:
    """Parse + validate one SKILL.md into a Playbook. Raises PlaybookError on a bad package."""
    skill_md = Path(skill_md)
    meta, body = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
    meta.setdefault("kind", KIND)
    errs = validate_meta(meta)
    if errs:
        raise PlaybookError(f"{skill_md}: invalid frontmatter — {'; '.join(errs)}")
    return Playbook(skill_md, meta, body)


def discover_playbooks(root: str | Path) -> list[Playbook]:
    """Walk *root* for ``SKILL.md`` packages and return the valid ones (fail-soft).

    Sits alongside ``Registry.discover_skills`` (typed ``.py`` tools). A broken package is
    skipped with a warning, never aborts discovery. Duplicate capability → keep the first.
    """
    base = Path(root)
    if not base.is_dir():
        return []
    out: list[Playbook] = []
    seen: set[str] = set()
    for skill_md in sorted(base.glob("**/SKILL.md")):
        try:
            pb = parse_playbook(skill_md)
        except (PlaybookError, OSError) as exc:
            logger.warning("playbook: skipping unloadable %s: %s", skill_md, exc)
            continue
        if pb.capability in seen:
            logger.warning("playbook: duplicate capability %r (%s) — keeping first",
                           pb.capability, skill_md)
            continue
        seen.add(pb.capability)
        out.append(pb)
    return out
