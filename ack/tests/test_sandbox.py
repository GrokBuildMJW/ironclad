"""#1464 F3b: model command isolation is mandatory and every unavailable/error path fails closed."""
from __future__ import annotations

import sys
import logging
from pathlib import Path

_CORE = Path(__file__).resolve().parents[2]
for p in (str(_CORE), str(_CORE / "engine")):
    if p not in sys.path:
        sys.path.insert(0, p)

import sandbox as sb  # noqa: E402


def test_best_effort_teardown_identifies_only_firejail():
    assert sb.is_best_effort_teardown("firejail") is True
    for backend in ("bwrap", "", "auto", "unknown"):
        assert sb.is_best_effort_teardown(backend) is False


def test_available_backend_specific_preference(monkeypatch):
    monkeypatch.setattr(sb.shutil, "which", lambda x: "/usr/bin/firejail" if x == "firejail" else None)
    assert sb.available_backend("firejail") == "firejail"
    assert sb.available_backend("bwrap") == ""                       # not on PATH
    assert sb.available_backend("auto") == "firejail"               # first available


def test_available_backend_auto_none(monkeypatch):
    monkeypatch.setattr(sb.shutil, "which", lambda x: None)
    assert sb.available_backend("auto") == ""


def test_wrap_firejail_isolates_network():
    w = sb.wrap_command("echo hi", backend="firejail")
    assert w.startswith("firejail") and "--net=none" in w and "'echo hi'" in w


def test_wrap_bwrap_isolates_network():
    w = sb.wrap_command("ls -la", backend="bwrap")
    assert w.startswith("bwrap") and "--unshare-net" in w and "'ls -la'" in w
    assert "--die-with-parent" in w and "--unshare-pid" in w
    assert "--dev-bind / /" in w and "--proc /proc" in w


def test_wrap_net_true_keeps_network():
    assert "--net=none" not in sb.wrap_command("x", backend="firejail", net=True)
    assert "--unshare-net" not in sb.wrap_command("x", backend="bwrap", net=True)


def test_wrap_unknown_or_empty_backend_refuses():
    import pytest
    with pytest.raises(ValueError):
        sb.wrap_command("echo hi", backend="")
    with pytest.raises(ValueError):
        sb.wrap_command("echo hi", backend="nope")


def test_wrap_quotes_embedded_single_quotes():
    w = sb.wrap_command("echo 'a'", backend="firejail")
    assert "'\\''" in w                                             # embedded quote escaped for sh -c


def test_sandbox_command_wraps_when_backend_present_else_typed_refusal(monkeypatch):
    monkeypatch.setattr(sb.shutil, "which", lambda x: "/usr/bin/firejail" if x == "firejail" else None)
    wrapped, backend = sb.sandbox_command("echo hi", "auto")
    assert backend == "firejail" and "firejail" in wrapped and "--net=none" in wrapped
    monkeypatch.setattr(sb.shutil, "which", lambda x: None)
    refused = sb.sandbox_command("echo hi", "auto")
    assert isinstance(refused, sb.SandboxRefusal)
    assert refused.preference == "auto" and "not available" in refused.reason


def test_engine_sandbox_policy_defaults_auto():
    import gx10
    assert gx10.SANDBOX == "auto"


def test_engine_firejail_advisory_is_log_only_and_emitted_once(monkeypatch, caplog):
    import gx10
    monkeypatch.setattr(gx10, "PLATFORM", "linux")
    monkeypatch.setattr(gx10, "_SANDBOX_BEST_EFFORT_WARNED", False)
    monkeypatch.setattr(sb, "sandbox_command", lambda *a, **k: ("wrapped-command", "firejail"))

    with caplog.at_level(logging.WARNING, logger="gx10"):
        assert gx10._sandbox_model_command("echo one") == ("wrapped-command", None)
        assert gx10._sandbox_model_command("echo two") == ("wrapped-command", None)

    records = [r for r in caplog.records if "best-effort-only" in r.getMessage()]
    assert len(records) == 1


def test_engine_bwrap_emits_no_best_effort_advisory(monkeypatch, caplog):
    import gx10
    monkeypatch.setattr(gx10, "PLATFORM", "linux")
    monkeypatch.setattr(gx10, "_SANDBOX_BEST_EFFORT_WARNED", False)
    monkeypatch.setattr(sb, "sandbox_command", lambda *a, **k: ("wrapped-command", "bwrap"))

    with caplog.at_level(logging.WARNING, logger="gx10"):
        assert gx10._sandbox_model_command("echo safe") == ("wrapped-command", None)

    assert not [r for r in caplog.records if "best-effort-only" in r.getMessage()]


def test_engine_no_backend_never_reaches_subprocess(monkeypatch):
    import gx10
    calls = []
    monkeypatch.setattr(gx10, "PLATFORM", "linux")
    monkeypatch.setattr(sb, "sandbox_command", lambda *a, **k: sb.SandboxRefusal("auto", "missing"))
    monkeypatch.setattr(gx10.subprocess, "run", lambda *a, **k: calls.append((a, k)))
    out = gx10.run_tool("execute_command", {"command": "echo must-not-run"})
    assert out.startswith("ERROR: execute_command refused") and "fails closed" in out
    assert calls == []


def test_engine_sandbox_import_or_wrapper_error_never_reaches_subprocess(monkeypatch):
    import gx10
    calls = []
    monkeypatch.setattr(gx10, "PLATFORM", "linux")
    monkeypatch.setattr(sb, "sandbox_command", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(gx10.subprocess, "run", lambda *a, **k: calls.append((a, k)))
    out = gx10.run_tool("execute_command", {"command": "echo must-not-run"})
    assert out.startswith("ERROR: execute_command refused") and "preparation failed" in out
    assert calls == []


def test_sandbox_policy_validation_and_legacy_tombstones(monkeypatch, capsys):
    import gx10
    cfg = gx10._code_defaults()
    cfg["security"]["sandbox"] = "off"
    gx10._apply_config(cfg)
    assert gx10.SANDBOX == "auto" and cfg["security"]["sandbox"] == "auto"
    assert "retired and ignored" in capsys.readouterr().out

    bad = gx10._code_defaults()
    bad["security"]["sandbox"] = "docker"
    import pytest
    with pytest.raises(ValueError, match="auto, bwrap, firejail"):
        gx10._apply_config(bad)


def test_sandbox_legacy_env_is_ignored(monkeypatch, capsys):
    import gx10
    monkeypatch.setenv("GX10_SANDBOX", "none")
    cfg = gx10._apply_env(gx10._code_defaults())
    assert cfg["security"]["sandbox"] == "auto"
    assert "GX10_SANDBOX=off/none is retired and ignored" in capsys.readouterr().out


def test_runtime_set_cannot_disable_or_set_unknown_sandbox(monkeypatch):
    import gx10
    lines = []
    gx10._EFFECTIVE_CFG = gx10._code_defaults()
    monkeypatch.setattr(gx10, "_ui_print", lambda value, *a, **k: lines.append(str(value)))
    gx10._dispatch(None, "config set security.sandbox off")
    gx10._dispatch(None, "config set security.sandbox docker")
    assert gx10._EFFECTIVE_CFG["security"]["sandbox"] == "auto"
    assert any("off/none is retired and ignored" in line for line in lines)
    assert any("must be one of: auto, bwrap, firejail" in line for line in lines)
