"""#1491: local coder launches have a hard wall-clock and whole-tree termination."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import client  # noqa: E402


def _item(template: str, *, timeout_s=None) -> dict:
    item = {
        "id": "TIMEOUT-1",
        "agent": "OPUS",
        "handover": "wait forever",
        "bin": sys.executable,
        "cmd_template": template,
        "tooling_envelope": {
            "enabled": True,
            "allow_list": [{"bin": sys.executable, "cmd_template": template}],
        },
    }
    if timeout_s is not None:
        item["timeout_s"] = timeout_s
    return item


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group proof")
def test_run_handover_timeout_kills_descendant_tree(tmp_path) -> None:
    sentinel = tmp_path / "descendant-wrote"
    ready = tmp_path / "descendant-started"
    writer = tmp_path / "writer.py"
    writer.write_text(
        "import pathlib, sys, time\n"
        "time.sleep(1.8)\n"
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
    # `{bin}` must stay a LITERAL placeholder (build_agent_argv fills it with sys.executable at launch);
    # escape it as `{{bin}}` so str.format only substitutes the positional path args (else KeyError: 'bin').
    template = "{{bin}} {} {} {} {}".format(
        parent.as_posix(), writer.as_posix(), sentinel.as_posix(), ready.as_posix()
    )

    started = time.monotonic()
    feedback, meta = client._run_handover(_item(template, timeout_s=1.0), tmp_path, log=lambda *_: None)
    elapsed = time.monotonic() - started

    assert feedback is None
    assert meta["exit_code"] is None and "timeout" in meta["stderr_tail"]
    assert elapsed < 4.0
    assert ready.exists(), "the descendant was spawned before the timeout"
    time.sleep(1.0)
    assert not sentinel.exists(), "a descendant survived the timed-out coder process group"


def test_run_handover_older_server_uses_default_timeout(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(client, "_CODER_TIMEOUT_DEFAULT", 0.2)
    template = "{bin} -c \"import time; time.sleep(10)\""

    started = time.monotonic()
    feedback, meta = client._run_handover(_item(template), tmp_path, log=lambda *_: None)

    assert feedback is None
    assert meta["exit_code"] is None and "timeout after 0s" in meta["stderr_tail"]
    assert time.monotonic() - started < 3.0
