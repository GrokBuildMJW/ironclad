"""#1500: opt-in proof that real bwrap reaps a setsid descendant."""
from __future__ import annotations

import shlex
import sys
import threading
import time
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402


def _setsid_tree_command(tmp_path: Path) -> tuple[str, Path, Path]:
    sentinel = tmp_path / "setsid-descendant-wrote"
    ready = tmp_path / "setsid-descendant-started"
    script = tmp_path / "setsid_tree.py"
    script.write_text(
        "import os, pathlib, sys, time\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    pathlib.Path(sys.argv[1]).write_text('ready', encoding='utf-8')\n"
        "    time.sleep(2.0)\n"
        "    pathlib.Path(sys.argv[2]).write_text('survived', encoding='utf-8')\n"
        "    os._exit(0)\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )
    return shlex.join([sys.executable, str(script), str(ready), str(sentinel)]), sentinel, ready


def _assert_descendant_reaped(sentinel: Path, ready: Path) -> None:
    assert ready.exists(), "the setsid descendant was spawned before termination"
    time.sleep(1.5)
    assert not sentinel.exists(), "a setsid descendant survived real bwrap namespace teardown"


def test_real_bwrap_timeout_reaps_setsid_descendant(monkeypatch, tmp_path) -> None:
    command, sentinel, ready = _setsid_tree_command(tmp_path)
    monkeypatch.setattr(gx10, "PLATFORM", "linux")
    monkeypatch.setattr(gx10, "SANDBOX", "bwrap")
    monkeypatch.setattr(gx10, "_EXEC_COMMAND_TIMEOUT_S", 1.0)

    out = gx10.run_tool("execute_command", {"command": command})

    assert out == "ERROR: Timeout after 1.0s"
    _assert_descendant_reaped(sentinel, ready)


def test_real_bwrap_cancel_reaps_setsid_descendant(monkeypatch, tmp_path) -> None:
    command, sentinel, ready = _setsid_tree_command(tmp_path)
    monkeypatch.setattr(gx10, "PLATFORM", "linux")
    monkeypatch.setattr(gx10, "SANDBOX", "bwrap")

    def cancel_after_spawn() -> None:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        gx10._CANCEL_EVENT.set()

    gx10._CANCEL_EVENT.clear()
    canceller = threading.Thread(target=cancel_after_spawn)
    canceller.start()
    try:
        out = gx10.run_tool("execute_command", {"command": command, "timeout": 10})
    finally:
        canceller.join(timeout=5)
        gx10._CANCEL_EVENT.clear()

    assert out == "ERROR: cancelled"
    _assert_descendant_reaped(sentinel, ready)
