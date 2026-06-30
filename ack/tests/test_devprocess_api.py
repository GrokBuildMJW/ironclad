from __future__ import annotations

import importlib

import pytest
from ack.devprocess import api


@pytest.fixture(autouse=True)
def _clear_driver():
    api.set_driver(None)
    yield
    api.set_driver(None)


class FakeDriver:
    def __init__(self):
        self.calls = {}

    def select_unit(self, candidates, *, skip=()):
        self.calls["select"] = (candidates, skip)
        return {"id": "u1"}

    def stage_handover(self, agent, handover_md, *, task_id=None, task_json=None, set_active=True, force=False):
        self.calls["stage"] = (agent, handover_md, task_id, task_json, set_active, force)
        return "staged"

    def record_feedback(self, task_id, agent, content):
        self.calls["fb"] = (task_id, agent, content)
        return "fb-path"

    def advance(self, task_id, agent, *, next_task_id=None):
        self.calls["adv"] = (task_id, agent, next_task_id)
        return "advanced"

    def deliver(self, unit, *, go, operator, secret, tree_sha, version, release_index, ledger_path, dial_config=None):
        self.calls["del"] = (unit, go, release_index, ledger_path)
        return (True, "delivered")


def test_import_and_version():
    importlib.reload(api)
    assert isinstance(api.__version__, str)
    assert api.__version__ != ""


def test_no_driver_degrades():
    assert api.get_driver() is None
    with pytest.raises(api.SubstrateUnavailable):
        api.select_unit([])
    with pytest.raises(api.SubstrateUnavailable):
        api.stage_handover("a", "md")
    with pytest.raises(api.SubstrateUnavailable):
        api.record_feedback("t", "a", "c")
    with pytest.raises(api.SubstrateUnavailable):
        api.advance("t", "a")
    with pytest.raises(api.SubstrateUnavailable):
        api.deliver({}, go="g", operator="o", secret=b"s", tree_sha="x", version="v", release_index="local-1", ledger_path="/l")


def test_protocol_runtime_checkable():
    assert isinstance(FakeDriver(), api.DevProcessDriver) is True


def test_set_get_driver():
    f = FakeDriver()
    api.set_driver(f)
    assert api.get_driver() is f
    api.set_driver(None)
    assert api.get_driver() is None


def test_verbs_delegate():
    f = FakeDriver()
    api.set_driver(f)

    assert api.select_unit([{"id": "u1"}], skip=("x",)) == {"id": "u1"}
    assert f.calls["select"] == ([{"id": "u1"}], ("x",))

    assert api.stage_handover("a", "md", task_json={"k": 1}) == "staged"
    assert f.calls["stage"] == ("a", "md", None, {"k": 1}, True, False)
    assert api.stage_handover("a", "md", task_id="T-1") == "staged"
    assert f.calls["stage"][2] == "T-1"

    assert api.record_feedback("t", "a", "c") == "fb-path"
    assert f.calls["fb"] == ("t", "a", "c")

    assert api.advance("t", "a", next_task_id="n") == "advanced"
    assert f.calls["adv"] == ("t", "a", "n")

    assert api.deliver(
        7,
        go="GO",
        operator="o",
        secret=b"s",
        tree_sha="x",
        version="v",
        release_index="local-1",
        ledger_path="/l",
    ) == (True, "delivered")
    assert f.calls["del"] == (7, "GO", "local-1", "/l")


def test_set_driver_none_restores_degrade():
    api.set_driver(FakeDriver())
    api.set_driver(None)
    with pytest.raises(api.SubstrateUnavailable):
        api.advance("t", "a")


def test_stage_handover_optional_task_id_degrades():
    with pytest.raises(api.SubstrateUnavailable):
        api.stage_handover(agent="a", handover_md="md", task_json={"k": 1})


def test_deliver_requires_ledger_path():
    api.set_driver(FakeDriver())
    with pytest.raises(TypeError):
        api.deliver(7, go="g", operator="o", secret=None, tree_sha="x", version="v", release_index="local-1")


def test_all_exports_present():
    for name in (
        "select_unit",
        "stage_handover",
        "record_feedback",
        "advance",
        "deliver",
        "set_driver",
        "get_driver",
        "SubstrateUnavailable",
        "DevProcessDriver",
        "__version__",
    ):
        assert name in api.__all__
