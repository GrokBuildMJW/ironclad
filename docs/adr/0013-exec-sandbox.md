# ADR-0013: OS-level execution sandbox for model commands

## Status

Accepted — mandatory and fail-closed.

## Context

Model `execute_command` runs generated shell code with the process identity. A missing isolation backend must
not silently turn a protected model tool into a host shell. The same rule must hold when the server bridges a
code-local command to a client.

## Decision

- `security.sandbox` / `GX10_SANDBOX` is a required backend policy: `auto` (default), `bwrap`, or `firejail`.
  Legacy `off`/`none` values warn and are ignored. Invalid values are refused. This live enum selects the
  mandatory backend; it is not an enable/disable switch for isolation.
- `sandbox_command()` returns either a wrapped command/backend pair or a typed `SandboxRefusal`; it never
  returns the original command as a fallback.
- Linux execution uses `bwrap --unshare-net --die-with-parent --unshare-pid` or `firejail --net=none`.
  Commands run in a dedicated process session; on timeout or cancellation the whole process group is killed.
  With **bwrap** the reap is complete: `--die-with-parent` + `--unshare-pid` make bwrap the namespace init, so
  killing it tears down every descendant — including a `setsid`/daemonized escapee. **firejail** tree teardown
  is explicitly best-effort-only: the engine performs a process-group kill plus a bounded reap, but a `setsid`
  descendant may survive. PID-death hardening for firejail was assessed and deferred because it has no clean
  die-with-parent equivalent, and a polling monitor would add races to the non-preferred fallback. Selecting
  firejail emits one process-deduplicated, non-blocking operator advisory on every topology that can run it:
  the native/Python-client sandbox preparer emits from `_sandbox_model_command`, while the Ink thin client
  emits when `findSandboxBackend` resolves firejail client-side. bwrap remains the preferred `auto` backend.
  The complete-tree guarantee applies to the normal bwrap timeout/cancel paths — an
  abnormal *engine* crash that bypasses the kill path can still orphan a detached sandboxed tree. Import,
  detection, and wrapper errors refuse before subprocess execution.
- Windows has no supported backend and refuses before Git Bash or PowerShell selection.
- Bridged model commands use a versioned wire-only name plus the validated backend policy. An older client
  cannot mistake the frame for the legacy direct-shell tool and therefore errors instead of executing.
- Accepted bridge-ceiling parity edge: near `ToolBridge`'s default 180-second timeout, Ink's bounded
  post-kill drain can delay its tool result by up to two seconds after the server has already returned the
  bridge-timeout string. The request id has already been removed, so the late result is a harmless no-op
  (a stale `410` that the client drops), not a hang; no timeout expansion is made here.
- Ink `/sh` remains a separate explicit operator channel. It is not a model tool and is never reachable from
  `execute_command`.

The production Linux host must install `bwrap` or `firejail`; `bwrap` is the preferred `auto` backend. Tests
use an opt-in command-wrapper shim for deterministic positive-path coverage and separately prove the real
no-backend path never reaches a subprocess. A real-bwrap `fork()` + `setsid()` descendant-reap proof covers
both timeout and cancellation only when `IRONCLAD_REAL_SANDBOX_TEST` is set and `bwrap` is present; the file is
not collected otherwise, so default offline counts and skip semantics stay honest. No CI runner provisioning
is treated as a security guarantee.

## Consequences

- Model command execution is unavailable on Windows and on Linux hosts without a supported backend.
- Network isolation prevents direct network access from the child, while the project filesystem remains
  accessible so build/test commands continue to work.
- Operator shell access and model tool authority are visibly separate.

## Remaining scope

- Full filesystem isolation with a private writable workspace.
- Seccomp and CPU/memory/PID ceilings.
- Container-runtime backends and per-tool policy.
- A Windows containment backend.
- Sandboxing the separately authorized coding-agent process itself.
