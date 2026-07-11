"""Pytest bootstrap for the ACK test suite.

Puts ``core/`` on ``sys.path`` so the tests can ``import ack`` (and ``ack.lodestar``)
regardless of the invocation directory.
"""
import sys
import copy
from collections import deque
from pathlib import Path

import pytest

# ack/tests/conftest.py → parents[2] == core/
CORE_DIR = Path(__file__).resolve().parents[2]
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))


_GX10_STATE_ATTRS = (
    "_BASE_CFG",
    "_EFFECTIVE_CFG",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "API_KEY_ENV",
    "STATE_ROOT",
    "VAULT_ROOT",
    "SESSION_FILE",
    "CODE_ROOT",
    "CODE_SUBDIR",
    "PLATFORM_MODE",
    "PLATFORM",
    "TASKS_DEDUP_THRESHOLD",
    "ONBOARDING_MODE",
    "TASK_PREFIX",
    "_TASK_ID_RE",
    "ACK_ENABLED",
    "LODESTAR_ENABLED",
    "FORGE_ENABLED",
    "FORGE_REPO",
    "FORGE_ADAPTER",
    "FORGE_TOKEN_ENV",
    "REVIEW_AGENT",       # #1221: default reviewer agent_id
    "REVIEW_TIMEOUT_S",  # #1221: review CLI timeout
    "NOTIFY_WEBHOOK",
    "AUDIT_ENABLED",
    "AUDIT_SCOPE",
    "INJECTION_DEFENSE",
    "SANDBOX",
    "MULTI_TENANT",
    "ALERT_ENABLED",
    "LLM_REQUEST_TIMEOUT_S",
    "LLM_CONNECT_TIMEOUT_S",
    "LLM_FIRST_TOKEN_TIMEOUT_S",
    "LLM_MAX_RETRIES",
    "AUTOPILOT_ENABLED",
    "AUTOPILOT_CLAUDE_BIN",
    "AUTOPILOT_EXTRA_ARGS",
    "AUTOPILOT_DEFAULT_EFFORT",
    "AUTOPILOT_LOGS_DIR",
    "AUTOPILOT_MAX_CONCURRENT",
    "AUTOPILOT_STREAM",
    "AUTOPILOT_TERMINATE_ON_ADVANCE",
    "AUTOPILOT_AUTOPLAN",
    "AUTOPILOT_MAX_TASKS",
    "_AUTOPLAN_DONE",
    "AUTOPILOT_LOG_TERMINAL",
    "TEMPERATURE",
    "MAX_TOKENS",
    "FINALIZE_ON_TRUNCATION",
    "RETRY_BACKOFF",
    "LANGUAGE",
    "MAX_ITERATIONS",
    "MAX_CTX_CHARS",
    "TRIM_TARGET_CHARS",
    "MAX_FILE_CHARS",
    "LIST_DIR_HARD_CAP",
    "SUMMARIZE_EVICTED",
    "SUMMARY_MAX_TOKENS",
    "RAG_ENABLED",
    "RAG_TOP_K",
    "RAG_MAX_TOKENS",
    "EMERGENCY_SUMMARIZE",
    "PROACTIVE_ROLL",
    "INGEST_SOFT_FRAC",
    "MAX_SUMMARIES_PER_TURN",
    "MAX_MODEL_LEN",
    "TOKEN_BUDGET",
    "CHARS_PER_TOKEN",
    "THINKING_RESERVE",
    "MEMORY_BRIEF_TOKENS",
    "MIN_OUTPUT_TOKENS",
    "OVERFLOW_SAFETY_TOKENS",
    "TURN_IDLE_TIMEOUT_S",
    "WORKER_MEMORY",
    "WORKER_WRITE",
    "WORKER_WRITE_MODE",
    "WARM_SESSION_ID",
    "_PLANNING_KW",
    "_ROUTINE_KW",
    "WORKSPACE_DIRS",
    "_IDLE_ACTIVE",
    "WATCHER_FEEDBACK_DIR",
    "_WATCHER_ENABLED",
    "RECONCILER_INTERVAL",
    "SPINNER_FRAMES",
    "UI_REFRESH_INTERVAL",
    "_UI_MAX_LINES",
    "_MEMORY_CONFIG",
    "_WARM_CONFIG",
    "_BOOT_WORKDIR",
    "STORE",
    "_REGISTRY",
    "_ACTIVE_PROJECT",
    "_MEMORY",
    "_WARM",
    "_TOKENS",
    "DESIGN_GATE_ENABLED",
    "CONSTRAINT_GATE_ENABLED",
    "CONSTRAINT_CONFLICT_DETECT",
    "ADVANCE_GATE_ENABLED",
    "AUTOMATION_DECOUPLED",
    "HEARTBEAT_STALL_S",
    "_NOTIFY_CONSUMER",
    "_QUALITY_BREAKER",
    "_LAST_VERDICT",
    "_VERIFY_GROUNDING_THRESHOLD",
    "_QUALITY_TRIPPED",
    "_STRATEGY_ENABLED",
    "_STRATEGY_BUDGET",
    "_LAST_STRATEGY",
    "_LAST_FAILURE_CLASS",
    "_AUTOPILOT_ACTIVE",
    "_UI_LINES",
    "_PROMPTS",
    "_PLAYBOOKS",
    "_PLUGIN_TOOLS",
)


def _snapshot_value(value):
    if value is None or isinstance(value, (str, int, float, bool, tuple, frozenset)):
        return value
    if isinstance(value, (dict, list, set)):
        return copy.deepcopy(value)
    if isinstance(value, deque):
        return deque(copy.deepcopy(list(value)), maxlen=value.maxlen)
    return value


def _restore_gx10_state(gx10, snapshot):
    for attr, value in snapshot.items():
        setattr(gx10, attr, _snapshot_value(value))


@pytest.fixture(autouse=True)
def _ace_isolation(request, tmp_path, monkeypatch):
    """Isolate gx10's process-global engine state around every ACK test.

    ACE is ALWAYS-ON: every ``gx10._apply_config`` registers a process-global PlaybookStore provider, starts a
    background ReflectionWorker daemon, and may migrate the legacy lesson tree under ``ironclad_home()``. The
    fixture also snapshots/restores the mutable gx10 config/runtime globals that ordinary tests can rebind or
    mutate in place. So, for every ACK test:

      * point ``ironclad_home`` at the test's tmp dir (no test ever touches the real install home) — except
        ``test_project_registry``, which tests ``ironclad_home`` itself;
      * after the test, restore the snapshotted gx10 engine globals, then stop/clear ACE workers, stores,
        providers, and injection state so the next test starts clean. ACE lifecycle fields are deliberately
        not snapshot-restored; the live worker refs must remain visible for this teardown to stop them.

    Known limitation: object singletons (``_WARM``, ``STORE``, ``_MEMORY``, ``_REGISTRY``, ``_TOKENS``,
    breaker/verdict objects) are rebind-only. In-place mutation of a live singleton is not deep-restored; a
    test that mutates one must reset it itself.

    Only resets modules that are already imported (no fresh import → no openai/select shadow surprises)."""
    gx10 = sys.modules.get("gx10")
    gx10_state = None
    pc_token = None
    if gx10 is not None:
        gx10_state = {
            attr: _snapshot_value(getattr(gx10, attr))
            for attr in _GX10_STATE_ATTRS
            if hasattr(gx10, attr)
        }
        pc = getattr(gx10, "_pc", None)
        if pc is not None:
            try:
                pc_token = pc.set_current(pc.current())
            except Exception:
                pc_token = None

    pr = sys.modules.get("project_registry")
    if pr is not None and request.module.__name__ != "test_project_registry":
        monkeypatch.setattr(pr, "ironclad_home", lambda: tmp_path, raising=False)
    yield
    gx10 = sys.modules.get("gx10")
    if gx10 is None:
        return
    if gx10_state is not None:
        _restore_gx10_state(gx10, gx10_state)
    for wattr in ("_ACE_WORKER", "_ACE_FORK_WORKER"):     # M5-2: also stop the fork MPR worker
        worker = getattr(gx10, wattr, None)
        if worker is not None:
            try:
                worker.stop()
            except Exception:
                pass
    for attr, val in (("_ACE_WORKER", None), ("_ACE_STORE", None), ("_ACE_MIGRATED", False),
                      ("_ACE_FORK_WORKER", None), ("_ACE_FORK_MPR", False)):
        if hasattr(gx10, attr):
            setattr(gx10, attr, val)
    if hasattr(gx10, "_ACE_INJECTED"):
        try:
            gx10._ACE_INJECTED.clear()
        except Exception:
            gx10._ACE_INJECTED = {}
    if hasattr(gx10, "_ACE_FORK_INFLIGHT"):
        try:
            gx10._ACE_FORK_INFLIGHT.clear()
        except Exception:
            pass
    if hasattr(gx10, "_ACE_CONSTRAINT_ENVELOPE_INFLIGHT"):
        try:
            gx10._ACE_CONSTRAINT_ENVELOPE_INFLIGHT.clear()
        except Exception:
            pass
    lessons = sys.modules.get("ack.lessons")
    pbs = sys.modules.get("playbook_store")
    if lessons is not None and pbs is not None:
        try:
            if isinstance(lessons.get_provider(), pbs.PlaybookStore):
                lessons.set_provider(None)
        except Exception:
            pass
    if pc_token is not None:
        try:
            gx10._pc.reset(pc_token)
        except Exception:
            pass
