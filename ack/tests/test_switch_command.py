from __future__ import annotations
import json
import sys
import os
from pathlib import Path
import pytest

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))
import gx10                     # noqa: E402
import project_context as pc     # noqa: E402


class FakeGx:
    def __init__(self):
        self.messages = [{"role": "system", "content": "SYS"}]
        self.last_response = ""
        self.fail_save = False

    def save_session(self, *, strict=False):
        if self.fail_save:
            if strict:
                raise OSError("disk full")
            return
        p = gx10.session_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"messages": self.messages}), encoding="utf-8")

    def load_session(self):
        p = gx10.session_path()
        if not p.exists():
            return 0
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return 0
        loaded = [m for m in data.get("messages", []) if m.get("role") != "system"]
        system = next((m for m in self.messages if m.get("role") == "system"), None)
        self.messages = ([system] if system else []) + loaded
        return len(loaded)


@pytest.fixture(autouse=True)
def _engine(tmp_path, monkeypatch):
    monkeypatch.setenv("GX10_HOME", str(tmp_path / "home"))
    old = os.getcwd()
    wd = tmp_path / "wd"
    wd.mkdir()
    os.chdir(wd)
    gx10._REGISTRY = None
    gx10._ACTIVE_PROJECT = None
    gx10._BOOT_WORKDIR = None
    pc.set_current(None)
    gx10.init_registry(wd)
    gx10._BASE_CFG = gx10._code_defaults()
    yield wd
    os.chdir(old)
    gx10._REGISTRY = None
    gx10._ACTIVE_PROJECT = None
    gx10._BOOT_WORKDIR = None
    pc.set_current(None)


def test_project_new_and_list(_engine):
    wd = _engine
    out = gx10._project_command("new acme --path " + str(wd / "acme"), FakeGx())
    assert "created acme" in out
    lst = gx10._project_command("list")
    assert "default" in lst and "acme" in lst


def test_switch_no_conversation_bleed(_engine):
    wd = _engine
    gx10._REGISTRY.register("acme", str(wd / "acme"))   # register-only (default stays active) to set up the switch
    ag = FakeGx()
    ag.messages.append({"role": "user", "content": "default-turn"})
    r = gx10._switch_command(ag, "acme")
    assert r.startswith("[switch] now on acme")
    assert pc.current().project_id == "acme" and pc.current().mem_ns
    assert ag.messages == [{"role": "system", "content": "SYS"}]
    assert (wd / ".ironclad" / "session.json").exists()


def test_switch_round_trip_isolation(_engine):
    wd = _engine
    gx10._REGISTRY.register("acme", str(wd / "acme"))   # register-only (default stays active) to set up the switch
    ag = FakeGx()
    ag.messages.append({"role": "user", "content": "default-turn"})
    gx10._switch_command(ag, "acme")
    ag.messages.append({"role": "user", "content": "acme-turn"})
    gx10._switch_command(ag, "default")
    assert pc.current().project_id == "default" and pc.current().mem_ns == ""
    contents = [m.get("content") for m in ag.messages]
    assert "default-turn" in contents and "acme-turn" not in contents
    acme_sess = json.loads(
        (wd / "acme" / ".ironclad" / "session.json").read_text(encoding="utf-8")
    )
    assert any(m.get("content") == "acme-turn" for m in acme_sess["messages"])


def test_switch_refused_when_inflight(_engine):
    wd = _engine
    gx10._REGISTRY.register("acme", str(wd / "acme"))   # register-only (default stays active) to set up the switch
    ag = FakeGx()
    lk = gx10._REGISTRY.project_lock("acme").acquire()
    try:
        r = gx10._switch_command(ag, "acme")
    finally:
        lk.release()
    assert r.startswith("[switch] refused")
    assert pc.current().project_id == "default"


def test_switch_unknown_project(_engine):
    ag = FakeGx()
    assert gx10._switch_command(ag, "ghost").startswith("[switch] unknown")


def test_switch_active_cache_updated(_engine):
    wd = _engine
    gx10._REGISTRY.register("acme", str(wd / "acme"))   # register-only (default stays active) to set up the switch
    ag = FakeGx()
    gx10._switch_command(ag, "acme")
    assert gx10._ACTIVE_PROJECT is not None and gx10._ACTIVE_PROJECT.id == "acme"
    assert gx10._REGISTRY.active().id == "acme"


def test_corrupt_target_session_no_bleed(_engine):
    wd = _engine
    gx10._REGISTRY.register("acme", str(wd / "acme"))   # register-only (default stays active) to set up the switch
    (wd / "acme" / ".ironclad").mkdir(parents=True, exist_ok=True)
    (wd / "acme" / ".ironclad" / "session.json").write_text("{ not json", encoding="utf-8")
    ag = FakeGx()
    ag.messages.append({"role": "user", "content": "LEAVING"})
    r = gx10._switch_command(ag, "acme")
    assert r.startswith("[switch] now on acme")
    assert ag.messages == [{"role": "system", "content": "SYS"}]


def test_rolling_summary_dropped_on_switch(_engine):
    wd = _engine
    gx10._REGISTRY.register("acme", str(wd / "acme"))   # register-only (default stays active) to set up the switch
    ag = FakeGx()
    ag.messages = [
        {"role": "system", "content": "SYS"},
        {"role": "system", "content": "ROLL"},
        {"role": "user", "content": "x"},
    ]
    gx10._switch_command(ag, "acme")
    assert ag.messages == [{"role": "system", "content": "SYS"}]


def test_last_response_cleared_on_switch(_engine):
    wd = _engine
    gx10._REGISTRY.register("acme", str(wd / "acme"))   # register-only (default stays active) to set up the switch
    ag = FakeGx()
    ag.last_response = "LEAVING-answer"
    gx10._switch_command(ag, "acme")
    assert ag.last_response == ""


def test_failed_leaving_save_aborts_switch(_engine):
    wd = _engine
    gx10._REGISTRY.register("acme", str(wd / "acme"))   # register-only (default stays active) to set up the switch
    ag = FakeGx()
    ag.fail_save = True
    r = gx10._switch_command(ag, "acme")
    assert r.startswith("[switch] failed")
    assert gx10._ACTIVE_PROJECT.id == "default"
