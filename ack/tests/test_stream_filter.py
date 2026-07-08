"""#1266 — the incremental stream filter must hide BOTH `<think>` and a model-emitted `<tool_call>` block
from the live render, across chunk boundaries, while leaving the RAW content (which feeds the post-turn
text→tool_call recovery) untouched. Pure unit tests of the parameterized `_ThinkFilter`.
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


def _feed_all(f, chunks) -> str:
    return "".join(f.feed(c) for c in chunks) + f.flush()


def test_think_block_suppressed_default():
    # back-compat: the default tags stay <think>…</think>.
    assert _feed_all(gx10._ThinkFilter(), ["a<think>secret</think>b"]) == "ab"


def test_toolcall_block_suppressed_parameterized():
    # #1266: the SAME filter, tags swapped, hides a text tool-call block from the render.
    out = _feed_all(gx10._ThinkFilter("<tool_call>", "</tool_call>"),
                    ['before<tool_call>{"name":"x"}</tool_call>after'])
    assert out == "beforeafter" and "tool_call" not in out


def test_suppression_across_chunk_boundaries():
    # the open AND close tags are split across chunks — nothing may leak.
    out = _feed_all(gx10._ThinkFilter("<tool_call>", "</tool_call>"),
                    ["a<tool", "_call>{", '"n":1}</tool', "_call>b"])
    assert out == "ab"


def test_plain_text_passes_through():
    assert _feed_all(gx10._ThinkFilter("<tool_call>", "</tool_call>"),
                     ["just text, no tags"]) == "just text, no tags"


def test_chained_think_then_toolcall():
    # this mirrors the stream loop: tf_tool.feed(tf.feed(x)) — both blocks vanish from the render.
    tf, tf_tool = gx10._ThinkFilter(), gx10._ThinkFilter("<tool_call>", "</tool_call>")
    src = 'x<think>reason</think>y<tool_call>{"n":1}</tool_call>z'
    rendered = tf_tool.feed(tf.feed(src)) + tf_tool.feed(tf.flush()) + tf_tool.flush()
    assert rendered == "xyz"


def test_entered_flag_flips_on_a_block():
    # the one-time render hint (text tool-call path) is driven by `.entered`.
    f = gx10._ThinkFilter("<tool_call>", "</tool_call>")
    assert f.entered is False
    f.feed("no tags yet")
    assert f.entered is False
    f.feed('<tool_call>{"n":1}</tool_call>')
    assert f.entered is True
