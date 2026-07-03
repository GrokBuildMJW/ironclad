# ADR-0013: OS-level execution sandbox for agent-run commands

## Status
Accepted — foundational increment (epic #1065 / #1069). **Default-off.**

## Context
`execute_command` runs agent-generated shell with the ORCHESTRATOR's own privileges — a runaway or hostile
command can exfiltrate data, phone home (C2), or trash the workspace. The credentials-ceiling note (#368)
points at OS isolation as the honest containment.

## Decision
A core sandbox seam (`engine/sandbox.py`), default-off:
- **`available_backend(preference)`** — detect an unprivileged Linux sandbox (bubblewrap / firejail) on PATH.
- **`wrap_command(command, backend)`** — build the isolation wrapper. The foundational win is **network
  isolation** (`firejail --net=none` / `bwrap --unshare-net`) — no exfiltration / no C2 callback — while the
  filesystem stays accessible so legitimate commands still work.
- Wired into `execute_command` (POSIX branch) via `security.sandbox = off | auto | bwrap | firejail`. When a
  backend is present the command runs isolated; when absent, it runs as-is — the seam never CLAIMS
  containment it cannot provide.

## Consequences
- The isolation is provided by the OS tool — it must be installed on the deploy host (Linux). On Windows the
  seam is a no-op (these tools don't exist).
- **Network-isolation-with-FS-access** is a deliberate first increment: full FS isolation would break
  commands that must read/write the project.

## Remaining scope (explicit — NOT faked here)
- Full filesystem isolation (read-only bind of the system + a private writable workspace).
- `seccomp` syscall filtering + resource (cpu / mem / pids) limits.
- A container-runtime (docker / podman) backend + per-tool policy.
- Sandboxing the launched code-agent process (`claude --print`), not just `execute_command`.
- A Windows containment story (restricted job object / low-privilege logon).
