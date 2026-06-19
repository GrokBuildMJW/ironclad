"""P0-5: parallel_reason routes through the provider router when active (gx10.py §6.2).

Default (no _DISPATCHER) is the byte-identical _WORKERS.fanout path (covered by test_parallel_tool).
Here: with an active ProviderDispatcher set on gx10, parallel_reason routes each item via the router
(idle → local Spark fanout; chat-busy → spill to the CLI runner) and renders results in order.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
import pytest  # noqa: E402
from providers import load_registry  # noqa: E402
from dispatch import ProviderDispatcher  # noqa: E402


class StubWorkers:
    def __init__(self):
        self.max_concurrency = 8
        self.calls = []

    def fanout(self, items, *, system=None, contexts=None, max_tokens=None, temperature=0.7, think=True):
        self.calls.append(list(items))
        return [{"ok": True, "content": f"spark:{it}", "error": None, "completion_tokens": 5} for it in items]


def _runner(spec, prompt, *, effort, max_tokens=None):
    return {"ok": True, "content": f"cli:{spec.provider_id}:{prompt}", "error": None, "completion_tokens": None}


SPARK = {"provider_id": "spark-vllm", "kind": "in-engine", "model": "m", "endpoint_env": "GX10_BASE_URL",
         "capabilities": {"local": True, "max_effort": "xhigh"}}
SONNET = {"provider_id": "claude-sonnet", "kind": "cli", "model": "sonnet", "bin": "claude",
          "cmd_template": "{bin} {prompt}", "capabilities": {"max_effort": "xhigh"}}


@pytest.fixture(autouse=True)
def _restore():
    w, d = gx10._WORKERS, gx10._DISPATCHER
    yield
    gx10._WORKERS, gx10._DISPATCHER = w, d


def _disp(workers, *, busy):
    reg = load_registry({"providers": {"pool": [SPARK, SONNET]}})
    d = ProviderDispatcher(reg, workers=workers, agent_runner=_runner, enabled=True)
    d.chat_busy_probe = (lambda: busy)
    return d


def test_inactive_dispatcher_uses_fanout_path():
    w = StubWorkers()
    gx10._WORKERS, gx10._DISPATCHER = w, None     # default → today's path
    out = gx10.run_tool("parallel_reason", {"items": ["a", "b"]})
    assert "[1] spark:a" in out and "[2] spark:b" in out
    assert w.calls == [["a", "b"]]                 # fanout used directly


def test_active_idle_routes_local_via_dispatcher():
    w = StubWorkers()
    gx10._WORKERS = w
    gx10._DISPATCHER = _disp(w, busy=False)        # idle → local Spark (through the dispatcher)
    out = gx10.run_tool("parallel_reason", {"items": ["a"]})
    assert "[1] spark:a" in out
    assert w.calls == [["a"]]                       # dispatcher still rides fanout for local items


def test_active_busy_spills_to_cli_runner():
    w = StubWorkers()
    gx10._WORKERS = w
    gx10._DISPATCHER = _disp(w, busy=True)         # chat-busy → spill to the CLI runner
    out = gx10.run_tool("parallel_reason", {"items": ["a", "b"]})
    assert "[1] cli:claude-sonnet:a" in out and "[2] cli:claude-sonnet:b" in out
    assert w.calls == []                            # Spark untouched — everything spilled to CLI
    assert out.splitlines()[0] == "[parallel_reason] 2/2 ok"


def test_active_dispatcher_still_validates_items():
    gx10._WORKERS = StubWorkers()
    gx10._DISPATCHER = _disp(gx10._WORKERS, busy=False)
    assert "non-empty list" in gx10.run_tool("parallel_reason", {"items": []})
