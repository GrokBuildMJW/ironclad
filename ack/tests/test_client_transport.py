"""Client-managed transport (client.Tunnel) — the Phase-d sealed-profile subprocess.

Exercises the generic tunnel runner WITHOUT SSH: a throwaway child process that binds the
target port stands in for the forward. Verifies address parsing, that __enter__ waits for
the port and __exit__ tears the child down, that an early-exiting command fails loudly, and
that the no-op stand-in works when no tunnel is configured.
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import pytest  # noqa: E402
import client  # noqa: E402

# Forward-slash exe so shlex.split (posix) doesn't eat Windows backslashes.
_PY = sys.executable.replace("\\", "/")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_addr_parsing():
    assert client.Tunnel("x", "http://localhost:8100")._addr() == ("localhost", 8100)
    assert client.Tunnel("x", "http://1.2.3.4:9001")._addr() == ("1.2.3.4", 9001)
    assert client.Tunnel("x", "http://host")._addr() == ("host", 8100)  # default port


def test_tunnel_up_then_torn_down():
    port = _free_port()
    code = (f"import socket,time;"
            f"s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
            f"s.bind(('127.0.0.1',{port}));s.listen();time.sleep(20)")
    cmd = f'"{_PY}" -c "{code}"'
    logs = []
    t = client.Tunnel(cmd, f"http://127.0.0.1:{port}", log=logs.append)
    with t:
        # Inside the context the local port accepts connections.
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
        assert t.proc is not None and t.proc.poll() is None
    # After exit the child is reaped — the deterministic teardown signal (the port
    # becoming refused again is OS-timing-dependent, so we don't assert on it).
    assert t.proc.poll() is not None
    assert any("transport up" in m for m in logs)


def test_early_exit_raises():
    # A command that exits before the port ever opens must fail loudly, not hang.
    cmd = f'"{_PY}" -c "import sys; sys.exit(3)"'
    t = client.Tunnel(cmd, f"http://127.0.0.1:{_free_port()}", log=lambda *_: None)
    with pytest.raises(RuntimeError, match="exited early"):
        t.__enter__()


def test_enter_failure_reaps_child():
    # A child that stays alive but never opens the port → __enter__ times out (raises)
    # and MUST terminate the child (__exit__ is not called when __enter__ raises).
    code = "import time; time.sleep(60)"            # alive, but binds nothing
    cmd = f'"{_PY}" -c "{code}"'
    # patch the deadline tiny so the test is fast
    import time as _t
    t = client.Tunnel(cmd, f"http://127.0.0.1:{_free_port()}", log=lambda *_: None)
    orig = client.time.monotonic
    start = orig()
    client.time.monotonic = lambda: orig() + 100   # force immediate timeout
    try:
        with pytest.raises(RuntimeError):
            t.__enter__()
    finally:
        client.time.monotonic = orig
    assert t.proc is not None and t.proc.poll() is not None   # child reaped, not orphaned


def test_null_ctx_is_noop():
    with client._NullCtx() as c:
        assert c is not None
