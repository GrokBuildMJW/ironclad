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


class LanguageFakeGx(FakeGx):
    def __init__(self):
        super().__init__()
        gx10.GX10._replace_language_guidance(self)

    def _replace_language_guidance(self):
        gx10.GX10._replace_language_guidance(self)


def _assert_language_directive_matches_effective_config(agent):
    language = gx10._EFFECTIVE_CFG["generation"]["language"]
    content = agent.messages[0]["content"]
    assert gx10.LANGUAGE == language
    assert gx10._language_guidance(language) in content
    assert content.count(gx10._LANGUAGE_GUIDANCE_MARKERS[0]) == 1


@pytest.fixture(autouse=True)
def _engine(tmp_path, monkeypatch):
    defaults = gx10._code_defaults()
    gx10._apply_config(defaults)
    runtime_defaults = gx10._snapshot_config_runtime()
    gx10._EFFECTIVE_CFG = defaults
    monkeypatch.setenv("GX10_HOME", str(tmp_path / "home"))
    old = os.getcwd()
    wd = tmp_path / "wd"
    wd.mkdir()
    os.chdir(wd)
    gx10._REGISTRY = None
    gx10._ACTIVE_PROJECT = None
    gx10._BOOT_WORKDIR = None
    monkeypatch.setattr(gx10, "_SESSION_OVERRIDES", {})
    pc.set_current(None)
    gx10.init_registry(wd)
    gx10._BASE_CFG = defaults
    yield wd
    os.chdir(old)
    gx10._REGISTRY = None
    gx10._ACTIVE_PROJECT = None
    gx10._BOOT_WORKDIR = None
    pc.set_current(None)
    gx10._restore_config_runtime(runtime_defaults)
    gx10._EFFECTIVE_CFG = defaults
    gx10._BASE_CFG = defaults


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


@pytest.mark.parametrize("target_has_session", [False, True])
def test_switch_rebuilds_language_directive_after_conversation_swap(
        _engine, monkeypatch, target_has_session):
    wd = _engine
    gx10._REGISTRY.register("acme", str(wd / "acme"))
    base = gx10._code_defaults()
    monkeypatch.setattr(gx10, "_BASE_CFG", base)
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", base)
    gx10._apply_config(base)
    ag = LanguageFakeGx()
    if target_has_session:
        target_session = wd / "acme" / ".ironclad" / "session.json"
        target_session.parent.mkdir(parents=True)
        target_session.write_text(
            json.dumps({"messages": [{"role": "user", "content": "saved target turn"}]}),
            encoding="utf-8",
        )

    gx10._dispatch(ag, "config set generation.language de")
    assert gx10.LANGUAGE == "de"
    assert "Always respond to the user in German" in ag.messages[0]["content"]

    result = gx10._switch_command(ag, "acme")

    assert result.startswith("[switch] now on acme")
    assert gx10.LANGUAGE == gx10._EFFECTIVE_CFG["generation"]["language"] == "de"
    assert "Always respond to the user in German" in ag.messages[0]["content"]
    assert "Always respond to the user in English" not in ag.messages[0]["content"]
    assert "could not re-apply" not in result


def test_same_project_reassert_rebuilds_language_directive(_engine, monkeypatch):
    base = gx10._code_defaults()
    monkeypatch.setattr(gx10, "_BASE_CFG", base)
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", base)
    gx10._apply_config(base)
    ag = LanguageFakeGx()
    gx10._dispatch(ag, "config set generation.language de")

    result = gx10._switch_command(ag, "default")

    assert result.startswith("[switch] now on default")
    assert gx10.LANGUAGE == gx10._EFFECTIVE_CFG["generation"]["language"] == "de"
    assert "Always respond to the user in German" in ag.messages[0]["content"]
    assert "Always respond to the user in English" not in ag.messages[0]["content"]
    assert "could not re-apply" not in result


def test_clearing_session_override_record_reverts_switch_to_base(_engine, monkeypatch):
    base = gx10._code_defaults()
    monkeypatch.setattr(gx10, "_BASE_CFG", base)
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", base)
    gx10._apply_config(base)
    gx10._dispatch(FakeGx(), "config set generation.language de")
    assert gx10._SESSION_OVERRIDES == {"generation.language": "de"}

    gx10._SESSION_OVERRIDES.clear()
    result = gx10._switch_command(FakeGx(), "default")

    assert result.startswith("[switch] now on default")
    assert gx10._EFFECTIVE_CFG["generation"]["language"] == "en"
    assert gx10.LANGUAGE == "en"


def test_session_override_wins_over_project_overlay(_engine, monkeypatch):
    base = gx10._code_defaults()
    monkeypatch.setattr(gx10, "_BASE_CFG", base)
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", base)
    monkeypatch.setattr(
        gx10, "_project_overlay_for", lambda _project: {"quality": {"threshold": 0.6}}
    )
    gx10._apply_config(base)
    gx10._dispatch(FakeGx(), "config set quality.threshold 0.7")

    result = gx10._switch_command(FakeGx(), "default")

    assert result.startswith("[switch] now on default")
    assert gx10._EFFECTIVE_CFG["quality"]["threshold"] == 0.7
    assert gx10._QUALITY_BREAKER.snapshot().threshold == 0.7


def test_context_session_overrides_rederive_across_switch(_engine, monkeypatch):
    base = gx10._code_defaults()
    monkeypatch.setattr(gx10, "_BASE_CFG", base)
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", base)
    gx10._apply_config(base)

    gx10._dispatch(FakeGx(), "config set context.token_budget off")
    gx10._switch_command(FakeGx(), "default")
    assert gx10._EFFECTIVE_CFG["context"]["token_budget"] is False
    assert gx10.TOKEN_BUDGET is False

    gx10._dispatch(FakeGx(), "config set context.max_ctx_chars 50000")
    gx10._switch_command(FakeGx(), "default")
    assert gx10._EFFECTIVE_CFG["context"]["token_budget"] is False
    assert gx10._EFFECTIVE_CFG["context"]["max_ctx_chars"] == 50000
    assert gx10.TOKEN_BUDGET is False
    assert gx10.MAX_CTX_CHARS == 50000


def test_switch_skips_and_reports_only_an_invalid_session_override(_engine, monkeypatch):
    base = gx10._code_defaults()
    monkeypatch.setattr(gx10, "_BASE_CFG", base)
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", base)
    gx10._apply_config(base)
    gx10._SESSION_OVERRIDES["quality.threshold"] = 2.0

    result = gx10._switch_command(FakeGx(), "default")

    assert "⚠ switch could not re-apply 1 runtime override: quality.threshold" in result
    assert gx10._EFFECTIVE_CFG["quality"]["threshold"] == base["quality"]["threshold"]
    assert gx10._SESSION_OVERRIDES == {"quality.threshold": 2.0}


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


def test_failed_target_config_rederive_restores_runtime_snapshot(_engine, monkeypatch):
    wd = _engine
    gx10._REGISTRY.register("acme", str(wd / "acme"))
    base = gx10._code_defaults()
    base["context"]["rag_enabled"] = True
    gx10._BASE_CFG = base
    gx10._apply_config(base)
    gx10._EFFECTIVE_CFG = base
    original_apply = gx10._apply_config_reconfiguration

    def fail_for_target(cfg, *, strict):
        if pc.current().project_id == "acme":
            gx10.RAG_ENABLED = False
            raise RuntimeError("target reconfiguration failed")
        return original_apply(cfg, strict=strict)

    monkeypatch.setattr(gx10, "_apply_config_reconfiguration", fail_for_target)
    result = gx10._switch_command(FakeGx(), "acme")

    assert result.startswith("[switch] failed")
    assert gx10._ACTIVE_PROJECT.id == "default"
    assert pc.current().project_id == "default"
    assert gx10._EFFECTIVE_CFG == base
    assert gx10.RAG_ENABLED is True


def test_failed_target_config_apply_keeps_language_directive_aligned(_engine, monkeypatch):
    wd = _engine
    gx10._REGISTRY.register("acme", str(wd / "acme"))
    base = gx10._code_defaults()
    base["generation"]["language"] = "de"
    monkeypatch.setattr(gx10, "_BASE_CFG", base)
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", base)
    gx10._apply_config(base)
    ag = LanguageFakeGx()
    before_language = gx10._EFFECTIVE_CFG["generation"]["language"]

    def fail_for_target(_cfg, *, strict):
        if pc.current().project_id == "acme":
            raise RuntimeError("target reconfiguration failed")

    monkeypatch.setattr(gx10, "_apply_config_reconfiguration", fail_for_target)

    result = gx10._switch_command(ag, "acme")

    assert result.startswith("[switch] failed")
    assert gx10._ACTIVE_PROJECT.id == "default"
    assert pc.current().project_id == "default"
    assert gx10._EFFECTIVE_CFG["generation"]["language"] == before_language
    _assert_language_directive_matches_effective_config(ag)


def test_failed_switch_commit_realigns_language_directive_after_rollback(_engine, monkeypatch):
    wd = _engine
    gx10._REGISTRY.register("acme", str(wd / "acme"))
    base = gx10._code_defaults()
    monkeypatch.setattr(gx10, "_BASE_CFG", base)
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", base)
    gx10._apply_config(base)
    ag = LanguageFakeGx()
    gx10._dispatch(ag, "config set generation.language de")

    def fail_commit(_pid):
        raise RuntimeError("registry commit failed")

    monkeypatch.setattr(gx10._REGISTRY, "set_active", fail_commit)

    result = gx10._switch_command(ag, "acme")

    assert result.startswith("[switch] failed")
    assert "registry commit failed" in result
    assert gx10._ACTIVE_PROJECT.id == "default"
    assert pc.current().project_id == "default"
    _assert_language_directive_matches_effective_config(ag)
