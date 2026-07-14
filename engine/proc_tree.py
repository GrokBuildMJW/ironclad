"""Best-effort process-tree termination shared by engine subprocess lanes."""
from __future__ import annotations

import os
import signal
import subprocess


def kill_process_tree(proc, *, windows=None) -> None:
    """Kill *proc* and its descendants, falling back to the direct child fail-soft."""
    is_windows = os.name == "nt" if windows is None else windows
    if is_windows:
        try:
            killed = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            if killed.returncode == 0:
                return
        except Exception:
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def drain_after_kill(proc, timeout):
    """Reap *proc* after a tree kill without allowing pipe drain to hang forever."""
    try:
        return proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        return ("", "")
