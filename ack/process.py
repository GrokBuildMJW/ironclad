"""Process-Level Self-Correction — the pure workflow-adaptation policy (Agent-Contract-Kernel, #602 S602-6).

> **Correct the PROCESS, not the response.** The re-ask loop fixes a single bad *output*; this adapts the
> *workflow* — which tool order succeeded, which retrieval query hit, which clarification was missing — so
> the NEXT task starts smarter. After a task, a structured :class:`ProcessSignal` is distilled into a typed
> :class:`ProcessLesson`; before the next turn, recent process-lessons are formatted into a compact hint.

This module is the **pure policy** (signal → lesson, lessons → hint): no transport, no model, no I/O, and it
**never raises** — so it is snapshot-testable. The *persistence* lives engine-side: the lesson is stored via
the **concrete** distiller provider (`engine.lesson_store.EngineLessonStore` — typed `record()`/`by_category`,
NOT the string-only `ack.lessons` seam, which can't round-trip typed fields, C0 fork-2). The engine maps the
:class:`ProcessLessonKind` to the provider's category. Imports only the stdlib.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence, Tuple


class ProcessLessonKind(str, Enum):
    """The kind of workflow lesson — the engine maps it to a concrete lesson category."""

    WORKING_PATH = "working_path"    # a workflow that succeeded → reuse the approach next time
    MISSING_INPUT = "missing_input"  # a clarification / context that was missing → gather it up front


@dataclass(frozen=True)
class ProcessSignal:
    """Structured WORKFLOW observations from a completed task (never the response content). Fields are
    best-effort — the engine fills what it can observe (today: task type / success / agent); richer signals
    (tool order, retrieval, missing clarification) layer in cleanly as they become available."""

    task_type: str = ""
    succeeded: bool = False
    agent: str = ""
    tools: Tuple[str, ...] = ()
    retrieval_hit: bool = False
    missing_clarification: str = ""


@dataclass(frozen=True)
class ProcessLesson:
    """A distilled, typed process-lesson: a :class:`ProcessLessonKind` + the human-readable note to store."""

    kind: ProcessLessonKind
    text: str


def distill_process_lesson(signal: ProcessSignal) -> Optional[ProcessLesson]:
    """Map a workflow :class:`ProcessSignal` to a typed :class:`ProcessLesson`, or ``None`` when nothing is
    actionable. Adapts the WORKFLOW (what worked / what was missing), never the response. Pure; **never
    raises**.

    Priority: a *missing clarification* is the most actionable signal (surface it first); else a *successful*
    task with a known type yields a reusable working-path note. A bare/typeless or unsuccessful signal with
    no missing-clarification yields ``None`` (nothing worth persisting)."""
    try:
        if not isinstance(signal, ProcessSignal):
            return None
        tt = (signal.task_type or "").strip()
        mc = (signal.missing_clarification or "").strip()
        if mc:
            where = f" for '{tt}' tasks" if tt else ""
            return ProcessLesson(ProcessLessonKind.MISSING_INPUT, f"Clarify up front{where}: {mc}")
        if signal.succeeded and tt:
            bits = []
            seq = " → ".join(t for t in (signal.tools or ()) if isinstance(t, str) and t)
            if seq:
                bits.append(f"tools: {seq}")
            if signal.retrieval_hit:
                bits.append("retrieval helped")
            agent = (signal.agent or "").strip()
            if agent:
                bits.append(f"agent: {agent}")
            detail = "; ".join(bits) if bits else "the prior approach worked"
            return ProcessLesson(ProcessLessonKind.WORKING_PATH,
                                 f"For '{tt}' tasks, a known working approach — {detail}.")
        return None
    except Exception:   # noqa: BLE001 — pure policy: any pathological signal → no lesson, never raise
        return None


def format_process_hint(
    texts: Optional[Sequence[str]],
    *,
    limit: int = 3,
    header: str = "Process notes (known working approaches from prior tasks):",
) -> str:
    """Format process-lesson *texts* into a compact pre-turn hint block; ``""`` when there is nothing (so the
    next turn is byte-identical when no process-lessons exist). Pure; **never raises**."""
    try:
        n = max(0, int(limit))
        items = [t for t in (texts or []) if isinstance(t, str) and t.strip()][:n]
        if not items:
            return ""
        return header + "\n" + "\n".join(f"- {t.strip()}" for t in items)
    except Exception:   # noqa: BLE001 — advisory hint: a formatting hiccup must never break a turn
        return ""
