"""Epic #366 follow-up (#428): the `ironclad` launcher must stop the local background engine on
client exit.

The launcher (`install/ironclad.ps1`) starts (or reuses) a local `server.py` and runs the
client; on `/exit` the client returns and the launcher's `finally` must reliably stop the engine —
whether THIS session started it or reused a running/orphaned one — so no background service lingers
on the port (the reported bug: `/exit` ended the CLI but the engine stayed reachable). The `.ps1` has
no unit harness, so this locks the behavior structurally.
"""
from __future__ import annotations

from pathlib import Path

_PS1 = Path(__file__).resolve().parents[2] / "install" / "ironclad.ps1"


def _text() -> str:
    return _PS1.read_text(encoding="utf-8")


def _finally_block() -> str:
    text = _text()
    assert "} finally {" in text, "the launcher must have a finally teardown"
    return text.split("} finally {", 1)[1]


def test_launcher_stops_local_engine_by_port_on_exit():
    # #428: the teardown stops the engine by its LISTENING PORT (not only the $started PID), so a
    # reused/orphaned engine is also stopped on /exit — no lingering background service.
    fin = _finally_block()
    assert "Get-NetTCPConnection" in fin
    assert "LocalPort $port" in fin
    assert "Stop-Process" in fin


def test_launcher_teardown_is_not_gated_only_on_started():
    # negative: the old teardown only ran `if ($started)`, so a reused engine was never stopped. The
    # stop-by-port must not sit behind an `if ($started)` guard.
    fin = _finally_block()
    stop_at = fin.index("Get-NetTCPConnection")
    preface = fin[:stop_at]
    assert "if ($started)" not in preface, "the stop-by-port must run regardless of $started (#428)"


def test_launcher_spark_path_exits_before_local_teardown():
    # the spark (thin-client) path returns before the try/finally — it must NOT stop a remote engine.
    text = _text()
    assert text.index("$type -eq 'spark'") < text.index("exit 0") < text.index("} finally {")


def test_launcher_still_reuses_a_healthy_same_version_engine():
    # regression: #428 changes ONLY the teardown — the start-side reuse (orphan-graceful) stays.
    text = _text()
    assert "reusing." in text and "$reuse = $true" in text
