"""Skill library catalogue (ADR-0001 D4, #35) — self-hosted, versioned, with provenance.

A thin index over the two discovery paths (`Registry.discover_skills` for typed `CASE`+`run`
tools, `discover_playbooks` for `SKILL.md` playbooks). It reads a per-skill **manifest** from
the skill itself (no separate registry file to drift): the shared metadata schema
(`capability`, `kind`, `version`, `type`, `domain`, `provenance`, `source`). Skills can be
**discovered**, **installed** (copied from one library into the active skills dir), and
**updated** (replaced when the source has a newer semver). No mandatory external marketplace —
provenance records origin; built-in vs user is just which library a skill lives in.

Zero external dependencies (stdlib only).
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DEFAULT_VERSION = "0.1.0"


def parse_version(v: str) -> tuple[int, ...]:
    """Lenient semver → comparable tuple; non-numeric parts → 0. Never raises."""
    parts: list[int] = []
    for chunk in str(v).split("-")[0].split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts) or (0,)


def is_newer(candidate: str, current: str) -> bool:
    """True if *candidate* is a strictly newer version than *current*."""
    return parse_version(candidate) > parse_version(current)


@dataclass
class SkillEntry:
    capability: str
    kind: str                       # "tool" | "playbook"
    description: str = ""
    version: str = DEFAULT_VERSION
    type: Optional[str] = None
    domain: Optional[str] = None
    provenance: str = "user"        # "built-in" | "user" | a source reference
    source: str = ""                # the file (tool) or dir (playbook) on disk
    library: str = ""               # the library root this entry came from

    def as_dict(self) -> dict:
        return {
            "capability": self.capability, "kind": self.kind, "description": self.description,
            "version": self.version, "type": self.type, "domain": self.domain,
            "provenance": self.provenance, "source": self.source, "library": self.library,
        }


def _entries_from_root(root: Path, *, provenance: str, library: str) -> list[SkillEntry]:
    from ack.playbook import discover_playbooks
    from ack.registry import Registry

    out: list[SkillEntry] = []
    # typed tools
    try:
        for reg in Registry().discover_skills(str(root)):
            meta = getattr(reg, "case", None) or {}
            out.append(SkillEntry(
                capability=reg.capability, kind="tool",
                description=str(getattr(reg, "description", "") or meta.get("description", "")),
                version=str(meta.get("version", DEFAULT_VERSION)),
                type=meta.get("type"), domain=meta.get("domain"),
                provenance=str(meta.get("provenance", provenance)),
                source=str(getattr(reg, "source", "")), library=library,
            ))
    except Exception:  # noqa: BLE001 — discovery is fail-soft; a bad root yields no tools
        pass
    # playbooks
    for pb in discover_playbooks(root):
        out.append(SkillEntry(
            capability=pb.capability, kind="playbook", description=pb.description,
            version=str(pb.meta.get("version", DEFAULT_VERSION)),
            type=pb.meta.get("type"), domain=pb.meta.get("domain"),
            provenance=str(pb.meta.get("provenance", provenance)),
            source=str(pb.dir), library=library,
        ))
    return out


@dataclass
class Catalogue:
    entries: dict[str, SkillEntry] = field(default_factory=dict)

    def add(self, entry: SkillEntry) -> None:
        existing = self.entries.get(entry.capability)
        if existing is None or is_newer(entry.version, existing.version):
            self.entries[entry.capability] = entry

    def get(self, capability: str) -> Optional[SkillEntry]:
        return self.entries.get(capability)

    def by_kind(self, kind: str) -> list[SkillEntry]:
        return [e for e in self.entries.values() if e.kind == kind]

    def by_domain(self, domain: str) -> list[SkillEntry]:
        return [e for e in self.entries.values() if e.domain == domain]

    def index(self) -> list[dict]:
        return [self.entries[c].as_dict() for c in sorted(self.entries)]


def build_catalogue(libraries: list[tuple[str, str]] | dict[str, str]) -> Catalogue:
    """Build a catalogue from *libraries* = list of (root, provenance) or {root: provenance}.

    Each root is scanned for both skill kinds; on a duplicate capability the higher version
    wins. ``provenance`` labels the library (e.g. ``built-in`` vs ``user``).
    """
    items = libraries.items() if isinstance(libraries, dict) else libraries
    cat = Catalogue()
    for root, provenance in items:
        for entry in _entries_from_root(Path(root), provenance=provenance, library=str(root)):
            cat.add(entry)
    return cat


def install(entry: SkillEntry, dest_root: str | Path, *, overwrite: bool = False) -> Path:
    """Install (copy) *entry* into *dest_root*/skills/. Returns the installed path.

    Refuses to overwrite an existing skill unless *overwrite*. File-first: a tool is a single
    `.py`; a playbook is its directory (copied whole).
    """
    dest_skills = Path(dest_root) / "skills"
    dest_skills.mkdir(parents=True, exist_ok=True)
    src = Path(entry.source)
    if entry.kind == "tool":
        target = dest_skills / src.name
        if target.exists() and not overwrite:
            raise FileExistsError(f"{target} exists (use overwrite=True)")
        shutil.copy2(src, target)
        return target
    target = dest_skills / src.name          # playbook dir name
    if target.exists():
        if not overwrite:
            raise FileExistsError(f"{target} exists (use overwrite=True)")
        shutil.rmtree(target)
    shutil.copytree(src, target)
    return target


def update(entry: SkillEntry, dest_root: str | Path) -> Optional[Path]:
    """Install *entry* over an installed skill **only if it is a newer version**.

    Returns the installed path if updated, else ``None`` (already up to date / not installed
    where install() would be the right call). Compares against whatever is currently in
    *dest_root* for the same capability + kind.
    """
    installed = build_catalogue([(str(Path(dest_root)), "user")]).get(entry.capability)
    if installed is not None and not is_newer(entry.version, installed.version):
        return None
    return install(entry, dest_root, overwrite=True)
