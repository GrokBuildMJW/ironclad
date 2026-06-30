"""``ack.devprocess.api`` — the curated, versioned public facade for the dev-process (ADR-0011 AD-3).

This is the SINGLE stable delegation target a generated per-project tool calls — ``select_unit``,
``stage_handover``, ``record_feedback``, ``advance``, ``deliver``. Everything else (the project registry,
the request-scoped ProjectContext, the state-machine driver, the deep-GitHub legs) is engine-internal and
may change without notice; only the names re-exported here are the public contract.

**Dependency inversion (why this stays in the wheel while the substrate does not).** The dev-process
implementation is NOT shipped in the ``ironclad-ai`` wheel — it lives in the engine (runnable scripts) and,
for hardened tiers, in a private extension reached over the existing driver seam. So this module imports
NOTHING from the engine: it exposes a tiny ``DevProcessDriver`` protocol and a process-global registration
seam (:func:`set_driver`). The engine registers a concrete driver at boot; a generated tool then calls the
stable verbs here and is delegated to whatever driver is wired. With NO driver registered — the wheel
installed on its own, no engine — every verb raises :class:`SubstrateUnavailable` with a clear message
instead of importing a missing internal module. Importing this facade always succeeds.

**Stability (ADR-0004).** Pre-1.0 the surface is provisional and may change between minor releases; from
1.0 it follows semver. Pin the :data:`__version__` you build a generated tool against. See
``docs/adr/0004-extension-sdk.md``.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional, Protocol, Sequence, runtime_checkable

#: Facade contract version (ADR-0004; provisional while < 1.0). Independent of the wheel version.
__version__ = "0.1.0"


class SubstrateUnavailable(RuntimeError):
    """Raised by a verb when no dev-process driver is registered — i.e. the engine substrate is not
    installed/active (e.g. the wheel imported on its own). Importing the facade never raises; only calling
    a verb without a wired driver does, with a clear, fail-closed message."""


@runtime_checkable
class DevProcessDriver(Protocol):
    """The seam a concrete dev-process implementation registers via :func:`set_driver`. The engine wires a
    driver at boot; the methods mirror the five public verbs. Signatures are the stable contract — a driver
    implements them, the facade only delegates."""

    def select_unit(self, candidates: "Iterable[dict]", *, skip: "Sequence[int]" = ()) -> "Optional[dict]": ...

    def stage_handover(self, agent: str, handover_md: str, *, task_id: "Optional[str]" = None,
                       task_json: "Optional[dict]" = None, set_active: bool = True,
                       force: bool = False) -> str: ...

    def record_feedback(self, task_id: str, agent: str, content: str) -> str: ...

    def advance(self, task_id: str, agent: str, *, next_task_id: "Optional[str]" = None) -> str: ...

    def deliver(self, unit: Any, *, go: Any, operator: str, secret: Any, tree_sha: str,
                version: str, release_index: str, ledger_path: str,
                dial_config: "Optional[dict]" = None) -> Any: ...


_DRIVER: "Optional[DevProcessDriver]" = None


def set_driver(driver: "Optional[DevProcessDriver]") -> None:
    """Register (or, with ``None``, clear) the process-global dev-process driver. The engine calls this at
    boot with its concrete implementation; tests pass a fake. Idempotent; last registration wins."""
    global _DRIVER
    _DRIVER = driver


def get_driver() -> "Optional[DevProcessDriver]":
    """The currently registered driver, or ``None`` when the substrate is not wired."""
    return _DRIVER


def _require() -> "DevProcessDriver":
    if _DRIVER is None:
        raise SubstrateUnavailable(
            "no dev-process driver is registered — the engine substrate is not installed/active "
            "(install + run Ironclad, or register a driver via ack.devprocess.api.set_driver)."
        )
    return _DRIVER


# ─── The five public verbs (thin delegation to the registered driver) ─────────
def select_unit(candidates: "Iterable[dict]", *, skip: "Sequence[int]" = ()) -> "Optional[dict]":
    """Pick the next unit of work from *candidates* (deterministic selection policy), skipping the issue
    *numbers* in *skip*. Returns the chosen unit dict or ``None`` when none is eligible."""
    return _require().select_unit(candidates, skip=skip)


def stage_handover(agent: str, handover_md: str, *, task_id: "Optional[str]" = None,
                   task_json: "Optional[dict]" = None, set_active: bool = True,
                   force: bool = False) -> str:
    """Stage a handover to *agent* with *handover_md* (validated at the contract gate). *task_id* is
    optional — omit it to create a new task (matching the engine tool's create-new shape). Returns a
    status line."""
    return _require().stage_handover(agent, handover_md, task_id=task_id, task_json=task_json,
                                     set_active=set_active, force=force)


def record_feedback(task_id: str, agent: str, content: str) -> str:
    """Record *agent*'s feedback for *task_id* (the signal the reconciler advances on). Returns the
    feedback path / a status line."""
    return _require().record_feedback(task_id, agent, content)


def advance(task_id: str, agent: str, *, next_task_id: "Optional[str]" = None) -> str:
    """Advance the pipeline past *task_id* (optionally activating *next_task_id*). Returns a status line."""
    return _require().advance(task_id, agent, next_task_id=next_task_id)


def deliver(unit: Any, *, go: Any, operator: str, secret: Any, tree_sha: str,
            version: str, release_index: str, ledger_path: str,
            dial_config: "Optional[dict]" = None) -> Any:
    """The delivery seam: authorize (supervised GO) + execute delivery for *unit*. Authorization binds the
    operator's *go* grant to the exact ``tree_sha``/``version``/``release_index`` and is recorded in the
    ledger at *ledger_path*; the driver performs the gated execution. Types are intentionally open (*unit*
    is the engine's unit handle, *go* the operator grant, *secret* the GO secret) so the driver can carry
    the engine's real delivery representation unchanged. Returns the driver's delivery result."""
    return _require().deliver(unit, go=go, operator=operator, secret=secret, tree_sha=tree_sha,
                              version=version, release_index=release_index, ledger_path=ledger_path,
                              dial_config=dial_config)


__all__ = [
    "__version__",
    "SubstrateUnavailable",
    "DevProcessDriver",
    "set_driver",
    "get_driver",
    "select_unit",
    "stage_handover",
    "record_feedback",
    "advance",
    "deliver",
]
