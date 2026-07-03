"""#1069: OS-level execution sandbox for agent-run commands.

`execute_command` runs agent-generated shell with the ORCHESTRATOR's own privileges. This wraps a command in
an OS isolation backend (bubblewrap / firejail) so a runaway or hostile command is CONTAINED — the
foundational win is **network isolation** (no exfiltration / no C2 callback), while the filesystem stays
accessible so legitimate commands still work. Pure command CONSTRUCTION + PATH detection here; the engine
gates it (default-off) and wires it into `execute_command`.

**The isolation is provided by the OS tool** — this seam builds the correct wrapper and refuses to claim
containment when no backend is present (`available_backend` → "" ⇒ the caller runs unsandboxed, honestly).
It is NOT a complete sandbox: full filesystem isolation, seccomp syscall filtering, a container runtime, and
per-tool policy are explicit remaining scope (see ADR-0013).
"""
from __future__ import annotations

import shutil
from typing import Optional

#: Supported backends, in auto-detect preference order. bubblewrap + firejail are the well-supported,
#: unprivileged Linux sandboxes; a full container runtime is remaining scope (ADR-0013).
BACKENDS = ("bwrap", "firejail")


def available_backend(preference: str = "auto") -> str:
    """The sandbox backend to use, or "" when none applies. A specific *preference* (``bwrap``/``firejail``)
    is honored only if that tool is on PATH; ``auto`` picks the first available; ``off``/``none``/"" ⇒ "".
    Reads PATH (via ``shutil.which``); never raises."""
    p = (preference or "").strip().lower()
    if p in ("", "off", "none", "disabled"):
        return ""
    try:
        if p in BACKENDS:
            return p if shutil.which(p) else ""
        for b in BACKENDS:                       # "auto" (or an unknown value) → first available
            if shutil.which(b):
                return b
    except Exception:   # noqa: BLE001
        return ""
    return ""


def _q(s: str) -> str:
    """Single-quote *s* for a POSIX shell ``-c`` argument (safe against embedded quotes)."""
    return "'" + (s or "").replace("'", "'\\''") + "'"


def wrap_command(command: str, *, backend: str, net: bool = False) -> str:
    """Build the shell command string that runs *command* under *backend*. Network is ISOLATED by default
    (``net=False``); the filesystem stays accessible so legit commands work. Pure (no execution). Returns
    *command* unchanged for an empty/unknown backend, so a caller that mis-gates fails OPEN to the unsandboxed
    command rather than silently dropping it (the engine only wraps when a backend is confirmed available)."""
    b = (backend or "").strip().lower()
    inner = _q(command)
    if b == "firejail":
        parts = ["firejail", "--quiet"]
        if not net:
            parts.append("--net=none")           # the containment win: no network for the child
        parts += ["--", "sh", "-c", inner]
        return " ".join(parts)
    if b == "bwrap":
        # bind the whole FS (so the command still works) but cut the network namespace when net is off.
        parts = ["bwrap", "--dev-bind", "/", "/", "--proc", "/proc"]
        if not net:
            parts.append("--unshare-net")
        parts += ["--", "sh", "-c", inner]
        return " ".join(parts)
    return command                               # unknown/off → unchanged (caller gates)


def sandbox_command(command: str, preference: str, *, net: bool = False) -> "tuple[str, str]":
    """Convenience for the engine: resolve the backend from *preference* and wrap *command*. Returns
    ``(wrapped_command, backend)`` — ``backend == ""`` means no sandbox was applied (run as-is)."""
    backend = available_backend(preference)
    if not backend:
        return command, ""
    return wrap_command(command, backend=backend, net=net), backend
