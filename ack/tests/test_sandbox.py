"""#1069 (epic #1065): OS-level execution sandbox for agent-run commands. Pure command-construction +
PATH-based backend detection; the foundational win is network isolation (no exfil / no C2) while the
filesystem stays accessible. Default-off; wired into execute_command's POSIX branch."""
from __future__ import annotations

import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parents[2]
for p in (str(_CORE), str(_CORE / "engine")):
    if p not in sys.path:
        sys.path.insert(0, p)

import sandbox as sb  # noqa: E402


def test_available_backend_off_and_specific_preference(monkeypatch):
    assert sb.available_backend("off") == "" and sb.available_backend("") == ""
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


def test_wrap_net_true_keeps_network():
    assert "--net=none" not in sb.wrap_command("x", backend="firejail", net=True)
    assert "--unshare-net" not in sb.wrap_command("x", backend="bwrap", net=True)


def test_wrap_unknown_or_empty_backend_is_unchanged():
    assert sb.wrap_command("echo hi", backend="") == "echo hi"
    assert sb.wrap_command("echo hi", backend="nope") == "echo hi"


def test_wrap_quotes_embedded_single_quotes():
    w = sb.wrap_command("echo 'a'", backend="firejail")
    assert "'\\''" in w                                             # embedded quote escaped for sh -c


def test_sandbox_command_wraps_when_backend_present_else_unchanged(monkeypatch):
    monkeypatch.setattr(sb.shutil, "which", lambda x: "/usr/bin/firejail" if x == "firejail" else None)
    wrapped, backend = sb.sandbox_command("echo hi", "auto")
    assert backend == "firejail" and "firejail" in wrapped and "--net=none" in wrapped
    monkeypatch.setattr(sb.shutil, "which", lambda x: None)
    cmd, b = sb.sandbox_command("echo hi", "auto")
    assert b == "" and cmd == "echo hi"                            # no backend → run as-is (never dropped)


def test_engine_sandbox_flag_defaults_off():
    import gx10
    assert gx10.SANDBOX == "off"
