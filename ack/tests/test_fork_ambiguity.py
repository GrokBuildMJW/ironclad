"""#1066 (epic #1065): Variant-B ambiguity auto-detector. The no-guessing rule is a prompt CONVENTION
(Variant A relies on the agent NOTICING the ambiguity). Variant B is the autonomous safety net: a pure,
precision-first pre-flight scan that flags requirement underspecification and emits a halt-to-ask ForkSignal,
wired (default-off) into the pre_handover Hook-Bus so an agent that didn't notice is stopped, not guessing."""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_CORE = Path(__file__).resolve().parents[2]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))
if str(_CORE / "engine") not in sys.path:
    sys.path.insert(0, str(_CORE / "engine"))       # gx10 lives under core/engine

from ack.ace.fork import ambiguity_signals, detect_ambiguity  # noqa: E402


def test_flags_uncertainty_markers():
    for txt in ["The API should return TBD", "handle it somehow", "not sure if we need auth", "figure out the format"]:
        assert ambiguity_signals(txt), txt
        assert detect_ambiguity(txt) is not None


def test_flags_open_question_and_multiple_interpretations():
    assert detect_ambiguity("Should the cache expire after 5m or 10m?") is not None      # '?'
    assert detect_ambiguity("Store it in either Redis or Postgres") is not None          # either/or
    assert detect_ambiguity("Retry the request as appropriate") is not None              # vague qualifier


def test_clear_requirements_are_not_flagged():
    for txt in ["Add a GET /metrics endpoint returning latency p50 and p95 as JSON.",
                "Cap docker json-file logs at 10MB with 3 rotations.",
                "Write the file to state_root/audit/ledger.jsonl."]:
        assert ambiguity_signals(txt) == [], txt
        assert detect_ambiguity(txt) is None


def test_forksignal_shape_and_fields():
    sig = detect_ambiguity("just do it somehow", unit="KGC-9", area="requirements")
    assert sig is not None and not sig.is_empty()
    assert sig.unit == "KGC-9" and sig.area == "requirements" and sig.question and sig.options


def test_empty_text_is_unambiguous():
    assert detect_ambiguity("") is None and ambiguity_signals("") == []
    assert ambiguity_signals(None) == []                                                 # type: ignore[arg-type]


def test_apply_ambiguity_registers_and_clears_the_hook():
    import gx10
    from ack import hooks
    before = len(hooks._HOOKS.get("pre_handover", ()))
    gx10._apply_ambiguity({"safety": {"ambiguity_detect": True}})
    assert len(hooks._HOOKS.get("pre_handover", ())) == before + 1
    gx10._apply_ambiguity({"safety": {"ambiguity_detect": False}})                       # default-off clears it
    assert len(hooks._HOOKS.get("pre_handover", ())) == before


def test_ambiguity_hook_warns_only_on_ambiguous(monkeypatch):
    import gx10
    printed = []
    monkeypatch.setattr(gx10, "_ui_print", lambda s: printed.append(s))
    gx10._ambiguity_hook({"handover_md": "just do it somehow", "task_id": "T1"})
    assert any("ambiguity" in str(p).lower() for p in printed)
    printed.clear()
    gx10._ambiguity_hook({"handover_md": "Add a GET /metrics endpoint returning p50/p95 latency as JSON.",
                          "task_id": "T2"})
    assert printed == []                                                                  # clear req → no warning
