"""#602 2.1 / #802 — engine integration tests for the mark-only Verifier on `pre_handover`.

Proves the Verifier actually RUNS on the dev-task pipeline (not a seam): with `verify.enabled` on, staging a
handover evaluates the task (behavioral rules over task_json + grounding of the handover claims via the cold
store) and stores a `VerdictResult` for the Quality breaker. MARK-ONLY (never gates the handover) + default-off
byte-identical (no hook registered → no verdict).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10
from ack import hooks


class _FakeMem:
    """A cold-store stand-in: `is_available()` true; `search` returns the canned hits."""

    def __init__(self, hits):
        self._hits = list(hits)
        self.searched = []

    def is_available(self):
        return True

    def search(self, q, limit=5):
        self.searched.append(q)
        return list(self._hits)


_GOOD_TASK = ('{"type":"feature","priority":"high","title":"Build the order service",'
              '"description":"Implement the full order service with validation, persistence and tests."}')
# a handover whose body line is a substantive claim (>= 30 chars, not a markdown header)
_HANDOVER = "## Handover\nbuild the order service end to end with full validation and tests"


@pytest.fixture(autouse=True)
def _reset():
    hooks.clear_hooks()
    gx10._set_last_verdict(None)
    yield
    hooks.clear_hooks()
    gx10._set_last_verdict(None)
    gx10._apply_config(gx10._code_defaults())   # restore defaults → verify off → hook unregistered


def _stage(tmp_path, monkeypatch, *, verify_on, mem=None, task_json=None, handover=_HANDOVER):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gx10, "_MEMORY", mem)        # None → grounding skipped; a _FakeMem → grounding runs
    cfg = gx10._code_defaults()
    if verify_on:
        cfg["verify"]["enabled"] = True
    gx10.STORE = None
    gx10._apply_config(cfg)
    gx10._dispatch(types.SimpleNamespace(run=lambda t: None, save_session=lambda: None, status=lambda: "ok"),
                   "initiative new Order Service --type software")
    return gx10._stage_handover(None, "OPUS", handover, task_json=task_json or _GOOD_TASK, force=True)


def test_verify_on_produces_a_handover_verdict(tmp_path, monkeypatch):
    out = _stage(tmp_path, monkeypatch, verify_on=True, mem=_FakeMem(["a prior order-service memory"]))
    assert out.startswith("OK")
    assert hooks.hook_count("pre_handover") == 1
    v = gx10._last_verdict()
    assert v is not None and v.verifier == "handover"
    assert v.passed and v.score == 1.0          # good task fields (rules 1.0) + everything grounded (1.0)


def test_grounding_failure_is_marked_not_gated(tmp_path, monkeypatch):
    out = _stage(tmp_path, monkeypatch, verify_on=True, mem=_FakeMem([]))   # nothing grounds
    assert out.startswith("OK")                  # MARK-ONLY: the handover still succeeds
    v = gx10._last_verdict()
    assert v is not None and not v.passed and v.score < 1.0   # grounding 0/1 drags the combined verdict down


def test_default_off_is_byte_identical_no_verdict(tmp_path, monkeypatch):
    out = _stage(tmp_path, monkeypatch, verify_on=False, mem=_FakeMem(["hit"]))
    assert out.startswith("OK")
    assert hooks.hook_count("pre_handover") == 0  # no hook registered
    assert gx10._last_verdict() is None           # nothing ran → byte-identical


def test_grounding_skipped_without_memory(tmp_path, monkeypatch):
    out = _stage(tmp_path, monkeypatch, verify_on=True, mem=None)   # _MEMORY None → grounding skipped
    assert out.startswith("OK")
    v = gx10._last_verdict()
    assert v is not None and v.verifier == "handover"   # rules-only verdict still produced
    assert v.passed and v.score == 1.0                  # good task fields, rules only


def test_rules_failure_on_thin_task_is_marked(tmp_path, monkeypatch):
    thin = '{"type":"feature","priority":"high","title":"Fix","description":"do it"}'   # title 1 word, desc < 40
    out = _stage(tmp_path, monkeypatch, verify_on=True, mem=None, task_json=thin)
    assert out.startswith("OK")                  # still mark-only
    v = gx10._last_verdict()
    assert v is not None and not v.passed and v.score < 1.0


class _RaisingMem:
    """A cold store that errors on every access — exercises the grounding fail-soft path."""

    def is_available(self):
        raise RuntimeError("memory down")

    def search(self, q, limit=5):
        raise RuntimeError("memory down")


def test_memory_error_keeps_the_rules_verdict(tmp_path, monkeypatch):
    # The Verifier runs at pre_handover (BEFORE the impl). A grounding-time memory error must drop ONLY
    # grounding — the already-computed rules verdict survives and is stored. (The impl's own memory brief
    # also errors on a fully-raising memory, so staging itself may ERROR — pre-existing + unrelated; what we
    # assert is that the Verifier did not lose its rules verdict to the grounding error — the #802 nit fix.)
    _stage(tmp_path, monkeypatch, verify_on=True, mem=_RaisingMem())
    v = gx10._last_verdict()
    assert v is not None and v.verifier == "handover"   # rules verdict survived the grounding error
    assert v.passed and v.score == 1.0                  # good task fields, rules-only (grounding dropped)


def test_disable_unregisters_the_hook(tmp_path, monkeypatch):
    _stage(tmp_path, monkeypatch, verify_on=True, mem=_FakeMem(["hit"]))
    assert hooks.hook_count("pre_handover") == 1
    gx10._apply_config(gx10._code_defaults())    # verify.enabled off
    assert hooks.hook_count("pre_handover") == 0  # cleanly unregistered (no sibling clobber)
