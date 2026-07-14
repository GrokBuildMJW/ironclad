"""Pytest bootstrap for the ACK test suite.

Puts ``core/`` on ``sys.path`` so the tests can ``import ack`` (and ``ack.lodestar``)
regardless of the invocation directory.
"""
import sys
import copy
import base64
import subprocess
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
    # F6a: schema-derived runtime policy maps are process globals and must not leak in-place test changes.
    "_FROZEN_CONFIG_KEYS",
    "_CONFIG_TOMBSTONES",
    "_CONFIG_ALIASES",
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
    "LODESTAR_ENABLED",
    "FORGE_ENABLED",
    "FORGE_REPO",
    "FORGE_ADAPTER",
    "FORGE_TOKEN_ENV",
    "REVIEW_AGENT",       # #1221: default reviewer agent_id
    "REVIEW_TIMEOUT_S",  # #1221: review CLI timeout
    "NOTIFY_WEBHOOK",
    "AUDIT_SCOPE",
    "_AUDIT_DEGRADED",
    "SANDBOX",
    "MULTI_TENANT",
    "TOOLING_ENVELOPE_POLICY",
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
    "_DESIGN_MIGRATION_BLOCKED",
    "FRAMING_NOTES_ENABLED",
    "AUTOMATION_DECOUPLED",
    "HEARTBEAT_STALL_S",
    "_NOTIFY_CONSUMER",
    "_QUALITY_BREAKER",
    "_LAST_VERDICT",
    "_VERIFY_GROUNDING_THRESHOLD",
    "_QUALITY_TRIPPED",
    "_STRATEGY_BUDGET",
    "_LAST_STRATEGY",
    "_LAST_FAILURE_CLASS",
    "_FAILURE_ATTEMPTS",   # F5b: strategy is always-on now → the per-task attempt counter must reset per test
                           # (else a leaked count escalates a later test's task into a durable blocked state)
    "_AUTOPILOT_ACTIVE",
    "_UI_LINES",
    "_PROMPTS",
    "_PLAYBOOKS",
    "_PLUGIN_TOOLS",
)


@pytest.fixture
def model_sandbox_backend(monkeypatch):
    """Deterministic positive-path backend for tests, independent of host bwrap/firejail provisioning.

    The product still resolves only real bwrap/firejail. This opt-in fixture replaces only the sandbox
    command-construction seam with a separate subprocess shim, so positive tests exercise wrapper dispatch
    and subprocess execution without making CI runner state a security assumption.
    """
    import gx10
    import sandbox

    shim = Path(__file__).with_name("sandbox_test_shim.py")

    def _prepare(command, preference, *, net=False):
        encoded = base64.b64encode(command.encode("utf-8")).decode("ascii")
        wrapped = subprocess.list2cmdline([sys.executable, str(shim), encoded])
        return wrapped, "test-sandbox-shim"

    monkeypatch.setattr(gx10, "PLATFORM", "linux")
    monkeypatch.setattr(sandbox, "sandbox_command", _prepare)
    return _prepare


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
        # F3a: mandatory audit preflight needs a writable hermetic ledger in positive tool tests. Redirect
        # only that ledger to a sibling of the test project: STATE_ROOT must remain project-scoped because
        # project/session isolation tests assert its real .ironclad routing.
        if request.module.__name__ not in {"test_audit_ledger", "test_audit_full"}:
            ledger = tmp_path.parent / f".{tmp_path.name}-audit" / "ledger.jsonl"
            monkeypatch.setattr(gx10, "_audit_ledger_path", lambda: ledger)
        # Positive coder fixtures inherit the real boot-derived launch policy. Tests of
        # refusal replace it explicitly; an absent policy is no longer an allow path.
        from ack.tooling_envelope import load_tooling_envelope_policy
        monkeypatch.setattr(gx10, "TOOLING_ENVELOPE_POLICY",
                            load_tooling_envelope_policy(gx10._code_defaults()))

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
                      ("_ACE_FORK_WORKER", None), ("_ACE_FORK_MPR", False),
                      # F5a: the quality breaker is ALWAYS-ON now (fed on every handover) and is a rebind-only
                      # singleton — reset its in-place state so a trip in one test never holds the next test's
                      # staging (the documented "reset a mutated singleton yourself" pattern).
                      ("_QUALITY_BREAKER", None), ("_QUALITY_TRIPPED", None), ("_LAST_VERDICT", None)):
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
