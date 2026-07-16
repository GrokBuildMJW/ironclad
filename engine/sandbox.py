"""#1069/#1464: mandatory OS-level isolation for model-issued commands.

`execute_command` runs agent-generated shell with the ORCHESTRATOR's own privileges. This wraps a command in
an OS isolation backend (bubblewrap / firejail) so a runaway or hostile command is CONTAINED — the
foundational win is **network isolation** (no exfiltration / no C2 callback), while the filesystem stays
accessible so legitimate commands still work. Pure command construction + PATH detection here; the engine
requires it for `execute_command`.

**The isolation is provided by the OS tool** — this seam builds the correct wrapper and returns a typed
refusal when no backend is present. It never returns the original command as a fallback.
It is NOT a complete sandbox: full filesystem isolation, seccomp syscall filtering, a container runtime, and
per-tool policy are explicit remaining scope (see ADR-0013).
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Union

#: Supported backends, in auto-detect preference order. bwrap stays first because its PID namespace
#: provides complete descendant teardown; firejail is the best-effort fallback (ADR-0013).
BACKENDS = ("bwrap", "firejail")


@dataclass(frozen=True)
class SandboxRefusal:
    """A fail-closed result: the model command must not reach a subprocess."""

    preference: str
    reason: str


def is_best_effort_teardown(backend: str) -> bool:
    """Whether *backend* cannot guarantee complete descendant teardown."""
    return (backend or "").strip().lower() == "firejail"


def available_backend(preference: str = "auto") -> str:
    """The sandbox backend to use, or "" when none applies. A specific *preference* (``bwrap``/``firejail``)
    is honored only if that tool is on PATH; ``auto`` picks the first available. Invalid and retired
    preferences select no backend. Reads PATH (via ``shutil.which``); never raises."""
    p = (preference or "").strip().lower()
    if p not in ("auto", *BACKENDS):
        return ""
    try:
        if p in BACKENDS:
            return p if shutil.which(p) else ""
        for b in BACKENDS:                       # auto → first available
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
    Raises for an empty/unknown backend so a caller cannot accidentally run the command unsandboxed."""
    b = (backend or "").strip().lower()
    inner = _q(command)
    if b == "firejail":
        # firejail has no clean die-with-parent equivalent; tree teardown is best-effort-only.
        parts = ["firejail", "--quiet"]
        if not net:
            parts.append("--net=none")           # the containment win: no network for the child
        parts += ["--", "sh", "-c", inner]
        return " ".join(parts)
    if b == "bwrap":
        # bind the whole FS (so the command still works) but cut the network namespace when net is off.
        parts = ["bwrap", "--die-with-parent", "--unshare-pid", "--dev-bind", "/", "/", "--proc", "/proc"]
        if not net:
            parts.append("--unshare-net")
        parts += ["--", "sh", "-c", inner]
        return " ".join(parts)
    raise ValueError(f"unsupported sandbox backend: {backend!r}")


def sandbox_command(
    command: str, preference: str, *, net: bool = False
) -> "Union[tuple[str, str], SandboxRefusal]":
    """Convenience for the engine: resolve the backend from *preference* and wrap *command*. Returns
    ``(wrapped_command, backend)`` on success or :class:`SandboxRefusal` when isolation is unavailable."""
    p = (preference or "").strip().lower()
    if p not in ("auto", *BACKENDS):
        return SandboxRefusal(p, f"unsupported sandbox policy {preference!r}")
    backend = available_backend(preference)
    if not backend:
        return SandboxRefusal(p, f"sandbox backend '{p}' is not available on PATH")
    try:
        return wrap_command(command, backend=backend, net=net), backend
    except Exception as exc:  # noqa: BLE001 — convert wrapper construction failure into a typed refusal
        return SandboxRefusal(p, f"sandbox wrapper construction failed: {exc}")
