"""#1489: model-command timeout/cancellation terminates the complete process tree."""
from __future__ import annotations

import os
import shlex
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402
import proc_tree  # noqa: E402


def _tree_command(tmp_path: Path) -> tuple[str, Path, Path]:
    sentinel = tmp_path / "descendant-wrote"
    ready = tmp_path / "descendant-started"
    writer = tmp_path / "writer.py"
    writer.write_text(
        "import pathlib, sys, time\n"
        "time.sleep(1.0)\n"
        "pathlib.Path(sys.argv[1]).write_text('survived', encoding='utf-8')\n",
        encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import pathlib, subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
        "pathlib.Path(sys.argv[3]).write_text('ready', encoding='utf-8')\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )
    return shlex.join([sys.executable, str(parent), str(writer), str(sentinel), str(ready)]), sentinel, ready


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group proof")
def test_execute_command_timeout_kills_descendant_tree(monkeypatch, tmp_path, model_sandbox_backend) -> None:
    command, sentinel, ready = _tree_command(tmp_path)
    # 1.0s (not 0.5): the chain is sh -> shim -> sh -> parent -> Popen(writer), two interpreter cold-starts,
    # so a tighter deadline races `ready` on a loaded self-hosted runner (#1489 review).
    monkeypatch.setattr(gx10, "_EXEC_COMMAND_TIMEOUT_S", 1.0)

    out = gx10.run_tool("execute_command", {"command": command})

    assert out == "ERROR: Timeout after 1.0s"
    assert ready.exists(), "the descendant was spawned before the timeout"
    time.sleep(0.8)
    assert not sentinel.exists(), "a descendant survived the timed-out process group"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group proof")
def test_execute_command_cancel_kills_descendant_tree(tmp_path, model_sandbox_backend) -> None:
    command, sentinel, ready = _tree_command(tmp_path)

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
    assert ready.exists(), "the descendant was spawned before cancellation"
    time.sleep(1.1)
    assert not sentinel.exists(), "a descendant survived cancellation of the process group"


def test_windows_tree_kill_uses_taskkill_force_tree(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    proc = SimpleNamespace(pid=4242, kill=lambda: pytest.fail("taskkill succeeded; direct kill is not expected"))
    monkeypatch.setattr(proc_tree.subprocess, "run", fake_run)

    gx10._kill_command_process_tree(proc, windows=True)

    assert calls == [["taskkill", "/F", "/T", "/PID", "4242"]]
