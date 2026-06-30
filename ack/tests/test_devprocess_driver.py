from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10                          # noqa: E402  (registers the engine driver at import)
from ack.devprocess import api       # noqa: E402


@pytest.fixture(autouse=True)
def _restore_driver():
    # re-register before (a sibling test may have cleared the global driver)
    # and after (so any set_driver(None) in a test never leaks)
    gx10._register_devprocess_driver()
    yield
    gx10._register_devprocess_driver()


def test_engine_driver_registered_at_import():
    d = api.get_driver()
    assert d is not None
    assert type(d).__name__ == "_EngineDevProcessDriver"
    assert isinstance(d, api.DevProcessDriver)


def test_facade_live_in_real_launch_shape():
    core_engine = str(Path(__file__).resolve().parents[2] / "engine")
    code = (
        "import sys\n"
        "sys.path[:] = [p for p in sys.path if p]\n"
        f"sys.path.insert(0, {core_engine!r})\n"
        "import gx10\n"
        "from ack.devprocess import api\n"
        "d = api.get_driver()\n"
        "print(type(d).__name__ if d is not None else 'NONE')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "_EngineDevProcessDriver", (out.stdout, out.stderr)


def test_register_does_not_clobber_existing_driver():
    class Rich:
        def select_unit(self, c, *, skip=()):
            return {"id": "r"}

        def stage_handover(self, *a, **k):
            return "rich"

        def record_feedback(self, *a, **k):
            return "rich"

        def advance(self, *a, **k):
            return "rich"

        def deliver(self, *a, **k):
            return "rich"

    from ack.devprocess import api

    rich = Rich()
    api.set_driver(rich)
    gx10._register_devprocess_driver()        # must NOT replace the richer driver
    assert api.get_driver() is rich
    # but when cleared, re-register installs the engine driver
    api.set_driver(None)
    gx10._register_devprocess_driver()
    assert type(api.get_driver()).__name__ == "_EngineDevProcessDriver"


def test_stage_and_advance_delegate(monkeypatch):
    seen = {}

    def fake_stage(task_id, agent, handover_md, task_json, set_active, force):
        seen["stage"] = (task_id, agent, handover_md, task_json, set_active, force)
        return "STAGED"

    def fake_adv(task_id, agent, next_task_id):
        seen["adv"] = (task_id, agent, next_task_id)
        return "ADV"

    monkeypatch.setattr(gx10, "_stage_handover", fake_stage)
    monkeypatch.setattr(gx10, "_advance_pipeline", fake_adv)

    assert api.stage_handover("ag", "md", task_json={"k": 1}) == "STAGED"
    assert seen["stage"] == (None, "ag", "md", {"k": 1}, True, False)   # task_id None -> impl arg0

    assert api.advance("T-1", "ag", next_task_id="T-2") == "ADV"
    assert seen["adv"] == ("T-1", "ag", "T-2")


def test_select_deliver_feedback_raise():
    with pytest.raises(api.SubstrateUnavailable):
        api.select_unit([])
    with pytest.raises(api.SubstrateUnavailable):
        api.deliver(1, go="g", operator="o", secret=None, tree_sha="x", version="v", release_index="local-1", ledger_path="/l")
    with pytest.raises(api.SubstrateUnavailable):
        api.record_feedback("t", "ag", "c")


def test_tool_dispatch_routes_through_facade(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        gx10,
        "_advance_pipeline",
        lambda task_id, agent, next_task_id: seen.__setitem__("adv", (task_id, agent, next_task_id)) or "ADV",
    )
    monkeypatch.setattr(
        gx10,
        "_stage_handover",
        lambda task_id, agent, handover_md, task_json, set_active, force: seen.__setitem__("stage", (task_id, agent, handover_md)) or "STAGED",
    )

    assert gx10.run_tool("advance_pipeline", {"task_id": "T-9", "agent": "ag"}) == "ADV"
    assert seen["adv"] == ("T-9", "ag", None)

    assert gx10.run_tool("stage_handover", {"agent": "ag", "handover_md": "md"}) == "STAGED"
    assert seen["stage"] == (None, "ag", "md")


def test_tool_dispatch_fallback_without_driver(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        gx10,
        "_advance_pipeline",
        lambda task_id, agent, next_task_id: seen.__setitem__("adv", (task_id, agent)) or "ADV",
    )
    api.set_driver(None)
    assert gx10.run_tool("advance_pipeline", {"task_id": "T-3", "agent": "ag"}) == "ADV"   # falls back to the direct impl
    assert seen["adv"] == ("T-3", "ag")
    # (the autouse fixture re-registers the engine driver afterwards)
