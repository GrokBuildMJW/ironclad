"""LOK-1: the orchestrator persists its LLM context after every real turn.

`save_session()` had no caller since the monolith CLI was removed (UNI-3): the server loaded
`.gx10_session.json` on boot but nothing ever wrote it, so the context was lost on every restart
(in local mode the orchestrator is ephemeral — one process per local-mode launch). The fix calls
`save_session()` in `_dispatch`'s real-turn branch (NOT for slash-commands).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402


def test_save_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # SESSION_FILE is cwd-relative (the project workdir)

    class A:
        _sanitize_messages = staticmethod(gx10.GX10._sanitize_messages)

    src = A()
    src.messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hallo"},
        {"role": "assistant", "content": "hi"},
    ]
    gx10.GX10.save_session(src)
    assert (tmp_path / gx10.SESSION_FILE).exists()

    dst = A()
    dst.messages = [{"role": "system", "content": "sys"}]  # the system prompt is kept, the rest reloaded
    n = gx10.GX10.load_session(dst)
    assert n == 2  # user + assistant (system is filtered out of the persisted set)
    assert [m["content"] for m in dst.messages if m["role"] != "system"] == ["hallo", "hi"]


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
