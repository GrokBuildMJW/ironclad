"""LOK-1: the orchestrator persists its LLM context after every real turn.

`save_session()` had no caller since the monolith CLI was removed (UNI-3): the server loaded
`.gx10_session.json` on boot but nothing ever wrote it, so the context was lost on every restart
(in local mode the orchestrator is ephemeral — one process per local-mode launch). The fix calls
`save_session()` in `_dispatch`'s real-turn branch (NOT for slash-commands).
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402


def test_save_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # session_path() resolves under state_root() (.ironclad/), workdir-relative

    class A:
        _sanitize_messages = staticmethod(gx10.GX10._sanitize_messages)

    src = A()
    src.messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hallo"},
        {"role": "assistant", "content": "hi"},
    ]
    gx10.GX10.save_session(src)
    sp = tmp_path / gx10.session_path()           # .ironclad/session.json under the workdir
    assert sp.exists()
    assert sp.parent.name == ".ironclad"          # A2: session lives in state_root, not the project root
    assert not (tmp_path / ".gx10_session.json").exists()  # old root-level location is gone

    dst = A()
    dst.messages = [{"role": "system", "content": "sys"}]  # the system prompt is kept, the rest reloaded
    n = gx10.GX10.load_session(dst)
    assert n == 2  # user + assistant (system is filtered out of the persisted set)
    assert [m["content"] for m in dst.messages if m["role"] != "system"] == ["hallo", "hi"]


def test_load_session_retains_rolling_summary_after_current_base_prompt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    persisted = {
        "messages": [
            {"role": "system", "content": "old base prompt"},
            {"role": "system", "content": gx10._SUMMARY_MARKER + "\nevicted conversation"},
            {"role": "user", "content": "current question"},
            {"role": "assistant", "content": "current answer"},
        ]
    }
    path = gx10.session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(persisted), encoding="utf-8")

    agent = gx10.GX10.__new__(gx10.GX10)
    agent.messages = [{"role": "system", "content": "current base prompt"}]

    assert agent.load_session() == 2
    assert agent.messages == [
        {"role": "system", "content": "current base prompt"},
        {"role": "system", "content": gx10._SUMMARY_MARKER + "\nevicted conversation"},
        {"role": "user", "content": "current question"},
        {"role": "assistant", "content": "current answer"},
    ]


def test_load_session_without_summary_replaces_only_base_prompt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    persisted = {
        "messages": [
            {"role": "system", "content": "old base prompt"},
            {"role": "user", "content": "current question"},
            {"role": "assistant", "content": "current answer"},
        ]
    }
    path = gx10.session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(persisted), encoding="utf-8")

    agent = gx10.GX10.__new__(gx10.GX10)
    agent.messages = [{"role": "system", "content": "current base prompt"}]

    assert agent.load_session() == 2
    assert agent.messages == [
        {"role": "system", "content": "current base prompt"},
        {"role": "user", "content": "current question"},
        {"role": "assistant", "content": "current answer"},
    ]


def test_failed_save_preserves_previous_session(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    class A:
        _sanitize_messages = staticmethod(gx10.GX10._sanitize_messages)

    prior = A()
    prior.messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "prior question"},
        {"role": "assistant", "content": "prior answer"},
    ]
    gx10.GX10.save_session(prior)
    sp = tmp_path / gx10.session_path()
    original = sp.read_text(encoding="utf-8")

    prior.messages.append({"role": "user", "content": "new turn"})
    replace_args = []

    def fail_replace(src, dst):
        replace_args.append((Path(src), Path(dst)))
        raise OSError("disk full")

    monkeypatch.setattr(gx10.os, "replace", fail_replace)
    gx10.GX10.save_session(prior, strict=False)

    assert "[WARN] session not saved: disk full" in capsys.readouterr().out
    assert replace_args[0][0].parent == sp.parent
    assert replace_args[0][1].resolve() == sp
    assert sp.read_text(encoding="utf-8") == original
    assert json.loads(original)["messages"][-1]["content"] == "prior answer"
    restored = A()
    restored.messages = [{"role": "system", "content": "sys"}]
    assert gx10.GX10.load_session(restored) == 2
    assert [m["content"] for m in restored.messages if m["role"] != "system"] == [
        "prior question", "prior answer"
    ]
    assert not list(sp.parent.glob(f"{sp.name}.*.tmp"))


def test_failed_save_strict_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class A:
        messages = [{"role": "user", "content": "turn"}]

    monkeypatch.setattr(gx10.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        gx10.GX10.save_session(A(), strict=True)
    assert not list((tmp_path / gx10.session_path()).parent.glob("*.tmp"))


class _FakeAgent:
    def __init__(self):
        self.ran = None
        self.saved = 0

    def run(self, text):
        self.ran = text

    def save_session(self):
        self.saved += 1

    def status(self):
        return "status-ok"


def test_dispatch_saves_after_real_turn():
    a = _FakeAgent()
    gx10._dispatch(a, "was ist die hauptstadt von frankreich?")
    assert a.ran == "was ist die hauptstadt von frankreich?"
    assert a.saved == 1  # persisted exactly once after the turn


def test_dispatch_command_does_not_save():
    a = _FakeAgent()
    gx10._dispatch(a, "status")  # a slash-command → no turn, no persist
    assert a.ran is None
    assert a.saved == 0
