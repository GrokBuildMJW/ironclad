"""#1183 (epic #1144): per-command shell routing in the engine — a PowerShell cmdlet runs in PowerShell, a
POSIX/bash command in Git Bash when installed, so BOTH shells work on Windows (neither forced). Mirrors
clients/ink/src/tools/shell.ts.
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


def test_detect_shell_powershell_vs_bash():
    assert gx10._detect_shell("Get-ChildItem") == "powershell"
    assert gx10._detect_shell("Get-ChildItem -Recurse") == "powershell"
    assert gx10._detect_shell("gci | Select-Object Name") == "powershell"
    assert gx10._detect_shell("$env:PATH") == "powershell"
    assert gx10._detect_shell("ls -la") == "bash"
    assert gx10._detect_shell("cd /x && ls -la") == "bash"
    assert gx10._detect_shell("grep -rn foo .") == "bash"
    assert gx10._detect_shell("git status") == "bash"  # shell-agnostic defaults to bash


def test_windows_guidance_opens_for_bash_only_when_git_bash_present():
    g = gx10._platform_guidance("windows")
    if gx10._git_bash():  # a real Windows box with Git Bash → both shells
        assert "bash" in g.lower()
    else:  # PowerShell-only (POSIX CI, or Windows without Git Bash)
        assert "PowerShell" in g and "NO Unix" in g


def test_linux_guidance_stays_posix():
    assert "POSIX/bash" in gx10._platform_guidance("linux")
