"""Boot-fixed setup.type → runner wiring (offload-topology.md, CL-1).

Two co-located modes: server (everything on the model host → in-engine only, byte-identical) and local
(engine + agents native on the desktop → local-subprocess runner; requires a REMOTE base_url + a reachable
CLI, else fail-closed). Unknown → fail-closed. security.profile=sealed forces server (no egress).
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
import pytest  # noqa: E402


def _cfg(setup_type="server", *, base_url="http://remote-gpu:8000/v1", profile="open"):
    return {"setup": {"type": setup_type}, "connection": {"base_url": base_url},
            "security": {"profile": profile}, "providers": {"enabled": False}}


def test_server_is_inactive_byte_identical():
    t = gx10.resolve_offload_topology(_cfg("server"))
    assert t == {"setup_type": "server", "providers_enabled": False, "runner_mode": "none"}


def test_default_missing_setup_is_server():
    t = gx10.resolve_offload_topology({"connection": {"base_url": "http://x:8000/v1"}})
    assert t["setup_type"] == "server" and t["runner_mode"] == "none"


def test_local_remote_with_cli_uses_local_runner():
    t = gx10.resolve_offload_topology(_cfg("local"), cli_available=True)
    assert t == {"setup_type": "local", "providers_enabled": True, "runner_mode": "local"}


@pytest.mark.parametrize("url", ["http://localhost:8000/v1", "http://127.0.0.1:8000/v1", ""])
def test_local_loopback_base_url_fails_closed(url):
    with pytest.raises(ValueError, match="REMOTE base_url"):
        gx10.resolve_offload_topology(_cfg("local", base_url=url), cli_available=True)


def test_local_without_cli_fails_closed():
    with pytest.raises(ValueError, match="agent CLI"):
        gx10.resolve_offload_topology(_cfg("local"), cli_available=False)


def test_unknown_setup_type_fails_closed():
    with pytest.raises(ValueError, match="unknown setup.type"):
        gx10.resolve_offload_topology(_cfg("pull"))   # pull/colocated/embedded no longer exist


# ── INSTALL-1 (#503): setup.type=auto derives the topology from base_url so a fresh default install boots ──
def test_auto_loopback_base_url_derives_server():
    # a fresh desktop default ships a loopback model → auto derives to in-engine server (boots, no config).
    t = gx10.resolve_offload_topology(_cfg("auto", base_url="http://127.0.0.1:8000/v1"))
    assert t["setup_type"] == "server" and t["providers_enabled"] is False and t["runner_mode"] == "none"
    assert "auto" in t.get("note", "")


def test_auto_remote_base_url_derives_local():
    # a remote model (the operator's working install) → auto derives to the LAN-offload local runner.
    t = gx10.resolve_offload_topology(_cfg("auto", base_url="http://remote-gpu:8000/v1"), cli_available=True)
    assert t == {"setup_type": "local", "providers_enabled": True, "runner_mode": "local",
                 "note": "setup.type=auto → local (remote base_url)"}


def test_auto_sealed_forces_server():
    # sealed overrides auto as well — no egress regardless of the derived topology.
    t = gx10.resolve_offload_topology(_cfg("auto", base_url="http://remote-gpu:8000/v1", profile="sealed"),
                                      cli_available=True)
    assert t["setup_type"] == "server" and t["runner_mode"] == "none"
    assert "sealed" in t.get("note", "")


def test_sealed_forces_server_over_local():
    # sealed = no egress → no external agents (force server/in-engine); never raises.
    t = gx10.resolve_offload_topology(_cfg("local", profile="sealed"), cli_available=True)
    assert t["setup_type"] == "server" and t["providers_enabled"] is False and t["runner_mode"] == "none"
    assert "sealed" in t.get("note", "")


def test_setup_type_default_is_server_in_code_defaults():
    assert gx10._code_defaults()["setup"]["type"] == "server"


def test_setup_type_is_frozen():
    assert "setup.type" in gx10._FROZEN_CONFIG_KEYS
