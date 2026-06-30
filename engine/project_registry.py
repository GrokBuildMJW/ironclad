"""Installation-global Project Registry — the SSOT of registered, isolated projects (ADR-0011, AD-6).

Generic project-isolation infrastructure: every engine session runs in a project (the implicit `default`
covers non-DEV use). This is engine runtime state, NOT in the wheel and NOT dev-process-specific — the
DEV target descriptor (exec_mode / release_index / protected-set / publish recipe) is a SEPARATE private
overlay keyed by project id, kept out of this public, generic registry.

Secret-free: the installation home resolves from ``GX10_HOME`` / a per-user default at runtime, never
hard-coded. Durability: atomic temp+fsync+replace (with a parent-dir fsync where supported) under an
**OS file lock** (``fcntl``/``msvcrt`` — released by the kernel on process death, so there is no stale
sentinel to reclaim and no cross-process mutual-exclusion race). The index is reconstructable from
on-disk project dirs (``reconcile``), so a lost/corrupt registry self-heals; one bad entry never bricks a
read. ``mem_ns`` is a minted >=64-bit memory-partition key, registry-verified unique, re-minted on a
duplicate or a low-entropy value — a copied project dir must never silently share a partition.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

if os.name == "nt":                                        # OS advisory lock primitives (auto-released on death)
    import msvcrt

    def _os_lock(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)            # non-blocking; OSError if already held

    def _os_unlock(fd: int) -> None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _os_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # non-blocking; OSError if already held

    def _os_unlock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass

DEFAULT_PROJECT_ID = "default"
_MEM_NS_BYTES = 8                                          # 8 bytes = 64-bit partition key (16 hex chars)
_LOCK_POLL_S = 0.02
_MEM_NS_RE = re.compile(r"^[0-9a-f]{16,}$")               # >=64-bit hex
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")  # no separators, no leading dot, no ".."


def ironclad_home() -> Path:
    """The installation-global home (reinstall-safe). ``GX10_HOME`` overrides; else a per-user default
    (``%LOCALAPPDATA%/ironclad`` on Windows, ``~/.ironclad`` elsewhere)."""
    env = os.environ.get("GX10_HOME")
    if env:
        return Path(env)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "ironclad"
    return Path.home() / ".ironclad"


def mint_mem_ns() -> str:
    """A fresh >=64-bit memory-partition key (uniqueness verified against the registry by the caller)."""
    return secrets.token_hex(_MEM_NS_BYTES)


def valid_mem_ns(ns: object) -> bool:
    return isinstance(ns, str) and bool(_MEM_NS_RE.match(ns))


def safe_id(value: object) -> bool:
    return isinstance(value, str) and value not in ("..", ".") and bool(_SAFE_ID_RE.match(value))


def _today() -> str:
    return date.today().isoformat()


def _fsync_dir(path: Path) -> None:
    """fsync a directory so a contained rename survives power loss (POSIX). No-op where unsupported."""
    try:
        dfd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)                                      # raises on Windows (dirs aren't fsync-able) — ignored
    except OSError:
        pass
    finally:
        os.close(dfd)


def _atomic_write(path: Path, data: str) -> None:
    """Crash-safe write: a unique temp file, fsync'd, then ``os.replace`` (atomic on POSIX + Windows),
    then a parent-dir fsync so the rename is durable. ``tmp`` != ``path``, so the cleanup never touches
    the real file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


class FileLock:
    """Cross-process advisory lock backed by an **OS file lock** (``fcntl.flock`` / ``msvcrt.locking``).
    The kernel releases it when the holder dies, so there is no stale reclaim and no race where one
    holder unlinks another's sentinel. The lock file persists (we lock a byte range, never unlink it)."""

    def __init__(self, path: "Path | str", *, timeout_s: Optional[float] = None,
                 poll_s: float = _LOCK_POLL_S) -> None:
        self.path = Path(path)
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self._fd: Optional[int] = None

    def acquire(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR)
        start = time.monotonic()
        while True:
            try:
                _os_lock(fd)
                self._fd = fd
                return self
            except OSError:
                if self.timeout_s is not None and (time.monotonic() - start) > self.timeout_s:
                    os.close(fd)
                    raise TimeoutError(f"could not acquire {self.path} within {self.timeout_s}s")
                time.sleep(self.poll_s)

    def release(self) -> None:
        if self._fd is not None:
            _os_unlock(self._fd)
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


@dataclass
class Project:
    """A registered, isolated project. Generic isolation fields only; the DEV target descriptor is a
    separate private overlay keyed by ``id``."""
    id: str
    slug: str
    root: str                                              # absolute path to the project root
    mem_ns: str                                            # >=64-bit memory partition key
    tracks: List[str] = field(default_factory=lambda: ["main"])
    active_track: str = "main"
    created: str = ""
    archived: bool = False                                 # hidden from the default project list; not switchable

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def try_from_dict(d: object) -> "Optional[Project]":
        """Tolerant parse: a malformed entry returns None (the reconciler drops it) rather than crashing
        a read of the whole registry."""
        if not isinstance(d, dict):
            return None
        try:
            pid, slug, root, mem_ns = d["id"], d["slug"], d["root"], d["mem_ns"]
        except (KeyError, TypeError):
            return None
        if not (isinstance(pid, str) and isinstance(slug, str) and isinstance(root, str)):
            return None
        tracks = d.get("tracks") or ["main"]
        if not isinstance(tracks, list):
            tracks = ["main"]
        return Project(id=pid, slug=slug, root=root, mem_ns=mem_ns if isinstance(mem_ns, str) else "",
                       tracks=[str(t) for t in tracks], active_track=str(d.get("active_track") or "main"),
                       created=str(d.get("created") or ""), archived=bool(d.get("archived", False)))


class Registry:
    """The installation-global SSOT. Every mutation runs read-modify-write under one OS file lock and
    writes atomically. Reads tolerate a malformed/partial index (bad entries are skipped)."""

    def __init__(self, home: "Optional[Path | str]" = None) -> None:
        self.home = Path(home) if home else ironclad_home()
        self.path = self.home / "registry.json"
        self.lock_path = self.home / "registry.lock"

    def project_lock(self, pid: str, **kw) -> FileLock:
        """A per-project lock (distinct from the registry lock). ``pid`` is validated to stay inside the
        locks dir (no path traversal)."""
        if not safe_id(pid):
            raise ValueError(f"unsafe project id for a lock path: {pid!r}")
        return FileLock(self.home / "locks" / f"{pid}.lock", **kw)

    def vault_lock(self, pid: str, track: str = "main", **kw) -> FileLock:
        """A per-project + per-track **vault-mutation** lock — distinct from :meth:`project_lock` (the
        dev-loop in-flight lock) so a quick reconcile is never mistaken for an in-flight dev unit
        (ADR-0011 AD-2': each track has its own lock). ``pid`` and ``track`` are validated to stay
        inside the locks dir (no path traversal)."""
        if not safe_id(pid):
            raise ValueError(f"unsafe project id for a vault lock path: {pid!r}")
        if not safe_id(track):
            raise ValueError(f"unsafe track for a vault lock path: {track!r}")
        return FileLock(self.home / "locks" / "vault" / pid / f"{track}.lock", **kw)

    # ---- read (tolerant) ----
    def load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {"projects": {}, "active": None}
        if not isinstance(raw, dict):
            return {"projects": {}, "active": None}
        entries = raw.get("projects")
        projects: Dict[str, Project] = {}
        if isinstance(entries, dict):
            for pid, d in entries.items():
                proj = Project.try_from_dict(d)
                if proj is not None and proj.id == pid:    # drop malformed / id-mismatched entries
                    projects[pid] = proj
        active = raw.get("active")
        if active not in projects:
            active = None
        return {"projects": projects, "active": active}

    def list(self) -> List[Project]:
        return list(self.load()["projects"].values())

    def get(self, pid: str) -> Optional[Project]:
        return self.load()["projects"].get(pid)

    def active(self) -> Optional[Project]:
        st = self.load()
        return st["projects"].get(st["active"]) if st["active"] else None

    # ---- write (all under one lock) ----
    def _save(self, state: dict) -> None:
        out = {
            "projects": {pid: p.to_dict() for pid, p in state["projects"].items()},
            "active": state.get("active"),
        }
        _atomic_write(self.path, json.dumps(out, indent=2, sort_keys=True))

    def _mutate(self, fn: Callable[[dict], object]) -> object:
        """Run ``fn(state)`` under the registry lock, then persist. ``fn`` returns the call's result."""
        with FileLock(self.lock_path):
            st = self.load()
            result = fn(st)
            self._save(st)
            return result

    @staticmethod
    def _unique_mem_ns(taken: set) -> str:
        while True:
            ns = mint_mem_ns()
            if ns not in taken:
                return ns

    def register(self, slug: str, root: "Path | str", *, project_id: Optional[str] = None,
                 created: Optional[str] = None, mem_ns: Optional[str] = None,
                 make_active: bool = False) -> Project:
        pid = project_id or slug
        if not safe_id(pid):
            raise ValueError(f"unsafe project id: {pid!r}")
        if not safe_id(slug):
            raise ValueError(f"unsafe slug: {slug!r}")
        root_abs = str(Path(root).resolve())

        def _do(st: dict) -> Project:
            projects = st["projects"]
            if pid in projects:
                raise ValueError(f"project id already registered: {pid}")
            for q in projects.values():
                if q.root == root_abs:
                    raise ValueError(f"root already registered (as {q.id!r}): {root_abs}")
            taken = {p.mem_ns for p in projects.values()}
            ns = mem_ns if (valid_mem_ns(mem_ns) and mem_ns not in taken) else self._unique_mem_ns(taken)
            proj = Project(id=pid, slug=slug, root=root_abs, mem_ns=ns, created=created or _today())
            projects[pid] = proj
            if make_active or st["active"] is None:
                st["active"] = pid
            return proj

        return self._mutate(_do)                            # type: ignore[return-value]

    def set_active(self, pid: str) -> None:
        def _do(st: dict) -> None:
            if pid not in st["projects"]:
                raise KeyError(f"unknown project: {pid}")
            st["active"] = pid
        self._mutate(_do)

    def remove(self, pid: str, *, expected_root: "Optional[str]" = None) -> "Optional[Project]":
        """Remove a project, returning the removed :class:`Project` (or ``None`` if it was not present /
        did not match). When *expected_root* is given the removal is **atomic against root reuse**: it is a
        no-op unless ``pid`` still exists AND still owns exactly that root — so a caller can then purge the
        returned project's directory without racing a concurrent re-registration of the same path."""
        def _do(st: dict) -> "Optional[Project]":
            p = st["projects"].get(pid)
            if p is None:
                return None
            if expected_root is not None and str(p.root) != str(expected_root):
                return None
            st["projects"].pop(pid, None)
            if st["active"] == pid:
                st["active"] = next(iter(st["projects"]), None)
            return p
        return self._mutate(_do)                            # type: ignore[return-value]

    def remove_purge(self, pid: str, expected_root: str) -> "Optional[Tuple[Project, str]]":
        """Atomically remove a project AND claim its directory for deletion, fully serialized against root
        reuse (ADR-0011 / S16). Under the registry lock: if *pid* still owns exactly *expected_root*, rename
        that root to a **fresh, unique** tombstone sibling (the rename *claims* the path so no concurrent
        re-registration can land there), drop the registry entry, and return ``(removed_project, tombstone)``;
        the caller ``rmtree``s the tombstone afterwards. Returns ``None`` (no change) if *pid* is absent or
        its root no longer matches. An ``OSError`` from the rename propagates WITHOUT removing the entry (so a
        dir that cannot be claimed is never recorded as deleted)."""
        def _do(st: dict) -> "Optional[Tuple[Project, str]]":
            p = st["projects"].get(pid)
            if p is None or str(p.root) != str(expected_root):
                return None
            src = Path(expected_root)
            tomb = None
            for i in range(100000):
                cand = src.with_name(f"{src.name}.__deleted__{'' if i == 0 else i}")
                if not cand.exists():
                    tomb = cand
                    break
            if tomb is None:
                raise OSError("no free tombstone path next to the project root")
            os.replace(src, tomb)                           # claim the dir while serialized; raises on failure
            st["projects"].pop(pid, None)
            if st["active"] == pid:
                st["active"] = next(iter(st["projects"]), None)
            return (p, str(tomb))
        return self._mutate(_do)                            # type: ignore[return-value]

    def add_track(self, pid: str, track: str) -> Project:
        """Register a new parallel track on a project (ADR-0011 AD-2' / S16). Idempotent — adding an
        existing track is a no-op. The active track is unchanged (use :meth:`set_active_track` to switch).
        Fail-closed on an unsafe track id or an unknown project."""
        if not safe_id(track):
            raise ValueError(f"unsafe track id: {track!r}")

        def _do(st: dict) -> Project:
            p = st["projects"].get(pid)
            if p is None:
                raise KeyError(f"unknown project: {pid}")
            if track not in p.tracks:
                p.tracks.append(track)
            return p

        return self._mutate(_do)                            # type: ignore[return-value]

    def set_archived(self, pid: str, value: bool) -> Project:
        """Mark a project archived / un-archived (ADR-0011 / S16). Archived projects are hidden from the
        default listing and refused as a switch target until un-archived; their data + memory are untouched
        (reversible). Fail-closed on an unknown project."""
        def _do(st: dict) -> Project:
            p = st["projects"].get(pid)
            if p is None:
                raise KeyError(f"unknown project: {pid}")
            p.archived = bool(value)
            return p

        return self._mutate(_do)                            # type: ignore[return-value]

    def set_active_track(self, pid: str, track: str) -> Project:
        """Switch a project's active track (ADR-0011 AD-2' / S16). Fail-closed: the track must already be
        registered (call :meth:`add_track` first), the id must be safe, and the project must exist."""
        if not safe_id(track):
            raise ValueError(f"unsafe track id: {track!r}")

        def _do(st: dict) -> Project:
            p = st["projects"].get(pid)
            if p is None:
                raise KeyError(f"unknown project: {pid}")
            if track not in p.tracks:
                raise ValueError(f"unknown track {track!r} for project {pid} (use 'track new' first)")
            p.active_track = track
            return p

        return self._mutate(_do)                            # type: ignore[return-value]

    def ensure_default(self, root: "Path | str") -> Project:
        """The implicit `default` project for non-DEV usage. The check-and-create runs under ONE lock,
        so concurrent callers converge on a single `default` (never None, never a duplicate). A
        missing/corrupt active pointer is repaired to `default` when no other project is active (never
        leaves the registry active-less while a default exists). The stored `default.root` is the
        first creator's workdir and is NOT re-pointed on later boots — the engine binds the `default`
        project to THIS process's own boot workdir (gx10._BOOT_WORKDIR), so a registry shared by boots
        from different workdirs can never re-point a running process's paths."""
        root_abs = str(Path(root).resolve())

        def _do(st: dict) -> Project:
            projects = st["projects"]
            existing = projects.get(DEFAULT_PROJECT_ID)
            if existing is not None:
                if st["active"] is None:                     # repair a missing/corrupt active pointer
                    st["active"] = DEFAULT_PROJECT_ID
                return existing
            ns = self._unique_mem_ns({p.mem_ns for p in projects.values()})
            proj = Project(id=DEFAULT_PROJECT_ID, slug=DEFAULT_PROJECT_ID, root=root_abs,
                           mem_ns=ns, created=_today())
            projects[DEFAULT_PROJECT_ID] = proj
            if st["active"] is None:
                st["active"] = DEFAULT_PROJECT_ID
            return proj

        return self._mutate(_do)                            # type: ignore[return-value]

    # ---- self-heal (ADR-0007 reconciler) ----
    def reconcile(self, *, disk_roots: "tuple | list" = ()) -> dict:
        """Validate + self-heal under the lock: re-mint a DUPLICATE or low-entropy mem_ns, flag a missing
        root, and rebuild entries for unregistered ``disk_roots`` (directories only; dot/`_` names
        skipped). Returns {reminted, quarantined, rebuilt}."""
        report: dict = {"reminted": [], "quarantined": [], "rebuilt": []}

        def _do(st: dict) -> None:
            projects = st["projects"]
            seen: Dict[str, str] = {}
            for pid in sorted(projects):                    # deterministic: first id keeps its ns
                p = projects[pid]
                if (not valid_mem_ns(p.mem_ns)) or (p.mem_ns in seen):
                    p.mem_ns = self._unique_mem_ns({q.mem_ns for q in projects.values()} | set(seen))
                    report["reminted"].append(pid)
                seen[p.mem_ns] = pid
                if not p.root or not Path(p.root).exists():
                    report["quarantined"].append(pid)
            known = {Path(p.root).resolve() for p in projects.values()}
            for r in disk_roots:
                rp = Path(r).resolve()
                if not rp.is_dir() or rp.name.startswith((".", "_")) or rp in known:
                    continue
                pid = rp.name
                if not safe_id(pid):
                    continue
                while pid in projects:
                    pid += "_"
                projects[pid] = Project(id=pid, slug=rp.name, root=str(rp),
                                        mem_ns=self._unique_mem_ns({q.mem_ns for q in projects.values()}),
                                        created=_today())
                known.add(rp)
                report["rebuilt"].append(pid)

        self._mutate(_do)
        return report

    def migrate_legacy_vaults(self, vault_root: "Path | str") -> List[str]:
        """Per-slug legacy migration (Codex S2): each ``vault/<slug>/meta.md`` becomes its OWN project,
        NOT one collapsed `default` (which would merge separate initiatives + break their isolation)."""
        vr = Path(vault_root)
        migrated: List[str] = []
        if not vr.is_dir():
            return migrated

        def _do(st: dict) -> None:
            projects = st["projects"]
            for sub in sorted(vr.iterdir()):
                if not sub.is_dir() or sub.name.startswith((".", "_")) or not safe_id(sub.name):
                    continue
                if not (sub / "meta.md").is_file() or sub.name in projects:
                    continue
                projects[sub.name] = Project(
                    id=sub.name, slug=sub.name, root=str(sub.resolve()),
                    mem_ns=self._unique_mem_ns({q.mem_ns for q in projects.values()}),
                    created=_today())
                migrated.append(sub.name)
            if migrated and st["active"] is None:
                st["active"] = migrated[0]

        self._mutate(_do)
        return migrated
