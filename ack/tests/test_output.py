"""Runtime-aware output (T-5): no encoding crashes, color only where it renders.

Covers the color decision (NO_COLOR / FORCE_COLOR / dumb / TTY), col() gating, the shared
setup_output(), and lossy tool reads so a non-UTF-8 file never crashes read_file.
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
import commands  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_color():
    prev = gx10._COLOR_ENABLED
    yield
    gx10._COLOR_ENABLED = prev


# ── col() gating ─────────────────────────────────────────────
def test_col_plain_when_disabled():
    gx10._COLOR_ENABLED = False
    assert gx10.col("hi", gx10.C.RED) == "hi"            # no escape codes


def test_col_colored_when_enabled():
    gx10._COLOR_ENABLED = True
    out = gx10.col("hi", gx10.C.RED)
    assert out.startswith(gx10.C.RED) and out.endswith(gx10.C.RESET) and "hi" in out


# ── _color_supported() ───────────────────────────────────────
class _FakeOut:
    def __init__(self, tty):
        self._tty = tty
    def isatty(self):
        return self._tty


def test_no_color_env_disables(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(gx10.sys, "stdout", _FakeOut(True))
    assert gx10._color_supported() is False


def test_force_color_overrides(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setattr(gx10.sys, "stdout", _FakeOut(False))   # not a tty…
    assert gx10._color_supported() is True                      # …but forced


def test_dumb_terminal_disables(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setattr(gx10.sys, "stdout", _FakeOut(True))
    assert gx10._color_supported() is False


def test_tty_enables_non_tty_disables(monkeypatch):
    for e in ("NO_COLOR", "FORCE_COLOR", "TERM"):
        monkeypatch.delenv(e, raising=False)
    monkeypatch.setattr(gx10.sys, "stdout", _FakeOut(True))
    assert gx10._color_supported() is True
    monkeypatch.setattr(gx10.sys, "stdout", _FakeOut(False))
    assert gx10._color_supported() is False


# ── setup_output() is failure-tolerant ───────────────────────
def test_setup_output_runs(monkeypatch):
    for e in ("NO_COLOR", "FORCE_COLOR", "TERM"):
        monkeypatch.delenv(e, raising=False)
    monkeypatch.setattr(gx10.sys, "stdout", _FakeOut(False))   # no reconfigure attr
    gx10._setup_output()                                        # must not raise
    assert gx10._COLOR_ENABLED is False


def test_commands_setup_output_runs():
    commands.setup_output()   # shared thin-client helper — must not raise


# ── lossy tool reads ─────────────────────────────────────────
def test_read_file_non_utf8_does_not_crash(tmp_path):
    f = tmp_path / "latin.txt"
    f.write_bytes(b"caf\xe9 \xff\xfe end")   # invalid UTF-8 bytes
    out = gx10.run_tool("read_file", {"path": str(f)})
    assert not out.startswith("ERROR")        # lossy decode, not a crash
    assert "caf" in out and "end" in out
