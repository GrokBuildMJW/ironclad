from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import project_switch as ps          # noqa: E402
import project_context as pc          # noqa: E402
from project_registry import Project  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_ctx():
    pc.set_current(None)
    yield
    pc.set_current(None)


class FakeReg:
    def __init__(self, log):
        self.p = {}
        self._active = None
        self.log = log

    def add(self, proj):
        self.p[proj.id] = proj

    def get(self, i):
        return self.p.get(i)

    def active(self):
        return self.p.get(self._active)

    def set_active(self, i):
        self._active = i
        self.log.append(("set_active", i))


class FakeAgent:
    def __init__(self, log, has_session):
        self.log = log
        self.has = has_session
        self.messages = ["OLD"]

    def save_session(self):
        cur = pc.current()
        self.log.append(("save", cur.project_id if cur else None))

    def load_session(self):
        self.log.append("load")
        if self.has:
            self.messages = ["LOADED"]
            return True
        return False

    def start_fresh(self, prompt_path):
        self.log.append(("fresh", prompt_path))
        self.messages = ["SYS"]


A = Project("A", "A", "/rootA", "aaaa1111bbbb2222")
B = Project("B", "B", "/rootB", "bbbb2222cccc3333")


def test_unknown_target_raises_keyerror():
    reg = FakeReg([])
    agent = FakeAgent([], False)
    with pytest.raises(KeyError):
        ps.switch_project(
            "X",
            registry=reg,
            agent=agent,
            base_cfg={},
            apply_config=lambda m: None,
        )


def test_refuses_when_target_in_flight():
    log = []
    reg = FakeReg(log)
    reg.add(A)
    reg.add(B)
    reg._active = "A"
    pc.set_current(pc.ProjectContext(A.id, A.root, A.mem_ns, "main"))
    agent = FakeAgent(log, True)
    with pytest.raises(ps.SwitchRefused):
        ps.switch_project(
            "B",
            registry=reg,
            agent=agent,
            base_cfg={},
            apply_config=lambda m: None,
            in_flight=lambda pid: pid == "B",
        )


def test_refuses_when_leaving_in_flight():
    log = []
    reg = FakeReg(log)
    reg.add(A)
    reg.add(B)
    reg._active = "A"
    pc.set_current(pc.ProjectContext(A.id, A.root, A.mem_ns, "main"))
    agent = FakeAgent(log, True)
    with pytest.raises(ps.SwitchRefused):
        ps.switch_project(
            "B",
            registry=reg,
            agent=agent,
            base_cfg={},
            apply_config=lambda m: None,
            in_flight=lambda pid: pid == "A",
        )


def test_save_sees_leaving_ctx_then_target_bound():
    log = []
    reg = FakeReg(log)
    reg.add(A)
    reg.add(B)
    reg._active = "A"
    pc.set_current(pc.ProjectContext(A.id, A.root, A.mem_ns, "main"))
    agent = FakeAgent(log, True)
    applied = {}
    base_cfg = {
        "connection": {"host": "spark"},
        "language": "en",
        "paths": {"system_prompt": "/p.md"},
    }

    target, dropped = ps.switch_project(
        "B",
        registry=reg,
        agent=agent,
        base_cfg=base_cfg,
        apply_config=lambda m: applied.update(m),
        overlay_for=lambda p: {"connection": {"host": "evil"}, "language": "de"},
    )

    assert log[0] == ("save", "A")
    assert pc.current().mem_ns == B.mem_ns
    assert applied["connection"]["host"] == "spark"
    assert applied["language"] == "de"
    assert "connection" in dropped
    assert agent.messages == ["LOADED"]
    assert log[-1] == ("set_active", "B")
    assert log.index("load") < log.index(("set_active", "B"))


def test_fresh_when_no_session():
    log = []
    reg = FakeReg(log)
    reg.add(A)
    reg.add(B)
    reg._active = "A"
    pc.set_current(pc.ProjectContext(A.id, A.root, A.mem_ns, "main"))
    agent = FakeAgent(log, False)
    base_cfg = {"paths": {"system_prompt": "/p.md"}}

    ps.switch_project(
        "B",
        registry=reg,
        agent=agent,
        base_cfg=base_cfg,
        apply_config=lambda m: None,
        overlay_for=lambda p: {"paths": {"system_prompt": "/q.md"}},
    )

    assert ("fresh", "/q.md") in log
    assert agent.messages == ["SYS"]


def test_leaving_none_no_save_but_activates():
    log = []
    reg = FakeReg(log)
    reg.add(B)
    agent = FakeAgent(log, True)
    applied = {}
    base_cfg = {"language": "en"}

    ps.switch_project(
        "B",
        registry=reg,
        agent=agent,
        base_cfg=base_cfg,
        apply_config=lambda m: applied.update(m),
        overlay_for=lambda p: {"language": "de"},
    )

    assert not any(entry[0] == "save" for entry in log if isinstance(entry, tuple))
    assert pc.current().project_id == "B"
    assert ("set_active", "B") in log
    assert applied


def test_same_project_rebuilds_config_no_session_churn():
    log = []
    reg = FakeReg(log)
    reg.add(B)
    reg._active = "B"
    pc.set_current(pc.ProjectContext(B.id, B.root, B.mem_ns, "main"))
    agent = FakeAgent(log, True)
    applied = {}
    base_cfg = {"language": "en"}

    ps.switch_project(
        "B",
        registry=reg,
        agent=agent,
        base_cfg=base_cfg,
        apply_config=lambda m: applied.update(m),
        overlay_for=lambda p: {"language": "de"},
    )

    assert not any(entry[0] == "save" for entry in log if isinstance(entry, tuple))
    assert "load" not in log
    assert not any(entry[0] == "fresh" for entry in log if isinstance(entry, tuple))
    assert ("set_active", "B") not in log
    assert pc.current().project_id == "B"
    assert applied.get("language") == "de"
    assert agent.messages == ["OLD"]


def test_failure_rolls_ctx_back_to_leaving():
    log = []
    reg = FakeReg(log)
    reg.add(A)
    reg.add(B)
    reg._active = "A"
    pc.set_current(pc.ProjectContext(A.id, A.root, A.mem_ns, "main"))
    agent = FakeAgent(log, True)

    def boom(m):
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        ps.switch_project(
            "B",
            registry=reg,
            agent=agent,
            base_cfg={"x": 1},
            apply_config=boom,
        )

    assert pc.current().project_id == "A"
    assert ("set_active", "B") not in log


def test_base_cfg_not_mutated():
    log = []
    reg = FakeReg(log)
    reg.add(A)
    reg.add(B)
    reg._active = "A"
    pc.set_current(pc.ProjectContext(A.id, A.root, A.mem_ns, "main"))
    agent = FakeAgent(log, True)
    base_cfg = {
        "connection": {"host": "spark"},
        "language": "en",
        "paths": {"system_prompt": "/p.md"},
    }
    original = {
        "connection": {"host": "spark"},
        "language": "en",
        "paths": {"system_prompt": "/p.md"},
    }

    ps.switch_project(
        "B",
        registry=reg,
        agent=agent,
        base_cfg=base_cfg,
        apply_config=lambda m: None,
        overlay_for=lambda p: {"connection": {"host": "evil"}, "language": "de"},
    )

    assert base_cfg == original


def test_ctx_for_injection_is_used():
    log = []
    reg = FakeReg(log); reg.add(A); reg.add(B); reg._active = "A"
    pc.set_current(pc.ProjectContext(A.id, A.root, A.mem_ns, "main"))
    agent = FakeAgent(log, True)
    seen = {}
    def ctx_for(p):
        ctx = pc.ProjectContext(p.id, p.root, "NS-" + p.id, "main")
        return ctx
    # capture the ctx mem_ns at save time
    def save():
        cur = pc.current(); seen["save_ns"] = cur.mem_ns if cur else None; log.append(("save", cur.project_id if cur else None))
    agent.save_session = save
    ps.switch_project("B", registry=reg, agent=agent, base_cfg={}, apply_config=lambda m: None, ctx_for=ctx_for)
    assert seen["save_ns"] == "NS-A"          # leaving saved under ctx_for(A)
    assert pc.current().mem_ns == "NS-B"      # bound to ctx_for(B)


def test_failed_commit_full_rollback():
    store = {"A": [{"role": "system", "content": "SYS"}, {"role": "user", "content": "A-conv"}], "B": []}
    applied = {"cfg": None}

    class Reg:
        def __init__(self):
            self._active = "A"
            self.p = {"A": A, "B": B}
        def get(self, i):
            return self.p.get(i)
        def active(self):
            return self.p.get(self._active)
        def set_active(self, i):
            raise RuntimeError("registry down")          # commit fails

    class Agent:
        def __init__(self):
            self.messages = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "A-conv"}]
        def _proj(self):
            cur = pc.current()
            return cur.project_id if cur else None
        def save_session(self):
            store[self._proj()] = list(self.messages)
        def load_session(self):
            data = store.get(self._proj()) or []
            sysm = [m for m in self.messages if m.get("role") == "system"][:1]
            self.messages = sysm + [m for m in data if m.get("role") != "system"]
            return bool([m for m in data if m.get("role") != "system"])
        def start_fresh(self, _p):
            self.messages = [m for m in self.messages if m.get("role") == "system"][:1]

    pc.set_current(pc.ProjectContext("A", "/rootA", "aaaa1111bbbb2222", "main"))
    ag = Agent()
    with pytest.raises(RuntimeError):
        ps.switch_project(
            "B",
            registry=Reg(),
            agent=ag,
            base_cfg={"connection": {"host": "h"}, "paths": {"system_prompt": "/p"}},
            apply_config=lambda m: applied.__setitem__("cfg", m),
            overlay_for=lambda p: {"tag": p.id},
        )
    # full rollback to the leaving project A
    assert pc.current().project_id == "A"
    assert any(m.get("content") == "A-conv" for m in ag.messages)
    assert applied["cfg"]["tag"] == "A"
