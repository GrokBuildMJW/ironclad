"""#1491/#1502: local coder launches have bounded capture and whole-tree termination."""
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


def test_bounded_tail_below_cap_passthrough() -> None:
    tail = client._BoundedTail()

    tail.append(b"plain stderr")

    assert tail.text() == "plain stderr"
    assert len(tail._tail) == len(b"plain stderr")


def test_bounded_tail_over_cap_keeps_marked_latest_bytes() -> None:
    tail = client._BoundedTail()
    payload = b"a" * 17 + b"b" * client._MAX_CAPTURE_BYTES

    tail.append(payload)

    assert len(tail._tail) == client._MAX_CAPTURE_BYTES
    assert tail.text() == client._TRUNCATED_MARKER + "b" * client._MAX_CAPTURE_BYTES


def test_bounded_tail_exact_boundary_uses_ink_greater_equal_semantics() -> None:
    tail = client._BoundedTail()
    payload = b"x" * client._MAX_CAPTURE_BYTES

    tail.append(payload)

    assert len(tail._tail) == client._MAX_CAPTURE_BYTES
    assert tail.text() == client._TRUNCATED_MARKER + payload.decode("utf-8")


def test_bounded_tail_multibyte_head_split_decodes_with_replacement() -> None:
    tail = client._BoundedTail()
    tail.append("€".encode("utf-8") + b"x" * (client._MAX_CAPTURE_BYTES - 4))

    tail.append(b"yy")
    text = tail.text()

    assert len(tail._tail) == client._MAX_CAPTURE_BYTES
    assert text.startswith(client._TRUNCATED_MARKER + "�")
    assert text.endswith("yy")


def test_drain_stderr_tolerates_closed_pipe_and_closes_it() -> None:
    class ClosedPipe:
        closed = False

        def read(self, _size):
            raise ValueError("read of closed file")

        def close(self) -> None:
            self.closed = True

    pipe = ClosedPipe()

    client._drain_stderr(pipe, client._BoundedTail())

    assert pipe.closed is True


def test_run_handover_bounds_flooded_stderr(tmp_path, monkeypatch) -> None:
    instances = []

    class TrackingTail(client._BoundedTail):
        def __init__(self) -> None:
            super().__init__()
            instances.append(self)

    monkeypatch.setattr(client, "_BoundedTail", TrackingTail)
    flood = tmp_path / "flood.py"
    flood.write_text(
        "import sys\n"
        f"sys.stderr.buffer.write(b'A' * ({client._MAX_CAPTURE_BYTES} + 65536) + b'LATEST')\n",
        encoding="utf-8",
    )
    template = '{{bin}} "{}"'.format(flood.as_posix())
    logs = []

    feedback, meta = client._run_handover(_item(template, timeout_s=5.0), tmp_path, log=logs.append)

    assert feedback is None
    assert meta["exit_code"] == 0 and len(meta["stderr_tail"]) <= client._STDERR_TAIL_CHARS
    assert meta["stderr_tail"].endswith("LATEST")
    assert len(instances) == 1 and len(instances[0]._tail) == client._MAX_CAPTURE_BYTES
    assert len(logs[1].encode("utf-8")) <= client._MAX_CAPTURE_BYTES + len(
        client._TRUNCATED_MARKER.encode("utf-8")
    )


def test_run_handover_timeout_drains_flooded_stderr_without_deadlock(tmp_path) -> None:
    flood = tmp_path / "flood-and-hang.py"
    flood.write_text(
        "import sys, time\n"
        f"sys.stderr.buffer.write(b'Z' * ({client._MAX_CAPTURE_BYTES} * 2))\n"
        "sys.stderr.buffer.flush()\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )
    template = '{{bin}} "{}"'.format(flood.as_posix())

    started = time.monotonic()
    feedback, meta = client._run_handover(_item(template, timeout_s=0.5), tmp_path, log=lambda *_: None)
    elapsed = time.monotonic() - started

    assert feedback is None
    assert meta == {"exit_code": None, "stderr_tail": "timeout after 0s"}
    assert elapsed < 4.0


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


if os.name == "nt":
    def test_run_handover_timeout_kills_descendant_tree_windows(tmp_path) -> None:
        if not os.environ.get("IRONCLAD_WIN_KILL_PROOF"):
            pytest.skip("set IRONCLAD_WIN_KILL_PROOF=1 to run the Windows taskkill /F /T proof")
        sentinel = tmp_path / "descendant-wrote-windows"
        ready = tmp_path / "descendant-started-windows"
        writer = tmp_path / "writer-windows.py"
        writer.write_text(
            "import pathlib, sys, time\n"
            "time.sleep(1.8)\n"
            "pathlib.Path(sys.argv[1]).write_text('survived', encoding='utf-8')\n",
            encoding="utf-8",
        )
        parent = tmp_path / "parent-windows.py"
        parent.write_text(
            "import pathlib, subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
            "pathlib.Path(sys.argv[3]).write_text('ready', encoding='utf-8')\n"
            "time.sleep(10)\n",
            encoding="utf-8",
        )
        template = '{{bin}} "{}" "{}" "{}" "{}"'.format(
            parent.as_posix(), writer.as_posix(), sentinel.as_posix(), ready.as_posix()
        )

        started = time.monotonic()
        feedback, meta = client._run_handover(
            _item(template, timeout_s=1.0), tmp_path, log=lambda *_: None
        )
        elapsed = time.monotonic() - started

        assert feedback is None
        assert meta["exit_code"] is None and "timeout" in meta["stderr_tail"]
        assert elapsed < 4.0
        assert ready.exists(), "the descendant was spawned before the timeout"
        time.sleep(1.0)
        assert not sentinel.exists(), "a descendant survived taskkill /F /T"
