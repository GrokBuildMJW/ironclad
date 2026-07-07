"""Epic #1130 / S2 (#1132) — Guard 1 watchdog: a stalled turn is aborted AND surfaced, never silent.

A per-turn IDLE watchdog resets on every progress signal (a generation chunk, a completed generation, a tool
result). If nothing progresses for TURN_IDLE_TIMEOUT_S the turn is aborted and re-labelled `stalled` so the one
deterministic turn-end marker names the cause ("⏱ TURN ABORTED — model stalled"). A slow-but-progressing turn
is never killed; disabled (<=0) is byte-identical.
"""
from __future__ import annotations

import sys
import time
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402


def _bare_agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gx10, "OpenAI", lambda **kw: object())
    g = gx10.GX10(base_url="http://x/v1", api_key="k", model="m", prompt_path="")
    g.messages = [{"role": "system", "content": "SYS"}]
    monkeypatch.setattr(g, "_classify_thinking", lambda _u: False)
    gx10._CANCEL_EVENT.clear()
    return g


def _capture_outcome(monkeypatch, g):
    seen = {}
    monkeypatch.setattr(g, "_print_turn_end", lambda turn, outcome: seen.update(outcome))
    return seen


def test_watchdog_trips_on_idle_stall_and_surfaces_stalled(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "TURN_IDLE_TIMEOUT_S", 0.3)
    g = _bare_agent(monkeypatch, tmp_path)
    seen = _capture_outcome(monkeypatch, g)

    def stalling_generate(think):
        time.sleep(0.7)                                   # NO progress signal for > the idle bound → watchdog trips
        return ("done", [], False, None, {})
    monkeypatch.setattr(g, "_generate", stalling_generate)

    try:
        g.run("do the thing")
    finally:
        gx10._CANCEL_EVENT.clear()

    assert g._watchdog_tripped is True                    # the watchdog fired
    assert seen.get("kind") == "stalled"                  # and the turn-end was re-labelled from abort → stalled
    assert "no progress" in (seen.get("detail") or "")    # with a cause, not a silent hang


def test_watchdog_does_not_trip_on_a_progressing_turn(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "TURN_IDLE_TIMEOUT_S", 5.0)
    g = _bare_agent(monkeypatch, tmp_path)
    seen = _capture_outcome(monkeypatch, g)
    monkeypatch.setattr(g, "_generate", lambda think: ("here is the answer", [], False, None, {}))

    g.run("quick question")

    assert g._watchdog_tripped is False                   # a fast, progressing turn is never killed
    assert seen.get("kind") == "done"


def test_watchdog_disabled_when_zero_is_byte_identical(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "TURN_IDLE_TIMEOUT_S", 0)   # off → no watchdog thread at all
    g = _bare_agent(monkeypatch, tmp_path)
    seen = _capture_outcome(monkeypatch, g)

    def slow_generate(think):
        time.sleep(0.4)                                   # a stall — but with the watchdog OFF it must NOT trip
        return ("answer", [], False, None, {})
    monkeypatch.setattr(g, "_generate", slow_generate)

    g.run("q")

    assert g._watchdog_tripped is False                   # disabled ⇒ no abort
    assert seen.get("kind") == "done"


def test_stalled_marker_is_in_the_turn_end_marks_map(monkeypatch, tmp_path):
    # the surfacing path: _print_turn_end must render a distinct, visible line for a stalled turn (not silence)
    g = _bare_agent(monkeypatch, tmp_path)
    printed = []
    monkeypatch.setattr(gx10, "_ui_print", lambda *a, **k: printed.append(" ".join(str(x) for x in a)))
    g._print_turn_end({"t0": time.time(), "gens": 1, "prompt": 0, "completion": 0},
                      {"kind": "stalled", "detail": "no progress for 240s"})
    blob = " ".join(printed)
    assert "ABORTED" in blob and "stalled" in blob        # a clear, surfaced marker — the operator is never blind


def test_turn_idle_timeout_config_default_and_env_override(monkeypatch):
    assert gx10._code_defaults()["context"]["turn_idle_timeout_s"] == gx10.TURN_IDLE_TIMEOUT_S
    saved = gx10.TURN_IDLE_TIMEOUT_S
    try:
        monkeypatch.setenv("GX10_TURN_IDLE_TIMEOUT_S", "17.5")
        gx10._apply_config(gx10._apply_env(gx10._code_defaults()))
        assert gx10.TURN_IDLE_TIMEOUT_S == 17.5
    finally:
        gx10.TURN_IDLE_TIMEOUT_S = saved                  # no cross-test bleed of the module global
