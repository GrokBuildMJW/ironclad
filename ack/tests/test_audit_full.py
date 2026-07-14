"""#1067 (epic #1065): the FULL, immutable, tamper-evident per-action audit log. Extends #1084's
audit_ledger — `audit.scope: all` records EVERY tool call (not just the mutating subset), each record carries
who/what/when/why (actor + action + target + reason + ts), and the agent's own write tools REFUSE the audit
directory so it can't tamper with its own trail (tamper-RESISTANCE beyond the hash-chain's tamper-EVIDENCE)."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import audit_ledger as al  # noqa: E402
import gx10  # noqa: E402


def test_record_action_captures_who_what_when_why(tmp_path):
    rec = al.record_action(tmp_path / "l.jsonl", "read_file", "a.py", ok=True, ts=42.0,
                           actor="orchestrator", reason="proj::main")
    p = rec["payload"]
    assert p["actor"] == "orchestrator" and p["action"] == "read_file" and p["detail"] == "a.py"
    assert p["reason"] == "proj::main" and p["ts"] == 42.0 and p["ok"] is True


def _audit_on(monkeypatch, tmp_path, scope):
    monkeypatch.setattr(gx10, "AUDIT_SCOPE", scope)
    monkeypatch.setattr(gx10, "state_root", lambda: tmp_path)
    monkeypatch.setattr(gx10, "_active_mem_ns", lambda: "proj::main")


def test_scope_all_records_every_tool_call(monkeypatch, tmp_path):
    _audit_on(monkeypatch, tmp_path, "all")
    gx10._maybe_audit("read_file", {"path": "a.py"}, "content")
    gx10._maybe_audit("query_memory", {"query": "how"}, "hits")
    gx10._maybe_audit("write_file", {"path": "b.py"}, "OK")
    recs = al.read_all(tmp_path / "audit" / "ledger.jsonl")
    assert [r["payload"]["action"] for r in recs] == ["read_file", "query_memory", "write_file"]
    assert all(r["payload"]["phase"] == "result" for r in recs)
    assert recs[0]["payload"]["reason"] == "proj::main"           # WHY = active scope
    assert recs[0]["payload"]["actor"] == "orchestrator"          # WHO
    assert al.verify_chain(tmp_path / "audit" / "ledger.jsonl") == []


def test_scope_mutating_skips_reads(monkeypatch, tmp_path):
    _audit_on(monkeypatch, tmp_path, "mutating")
    gx10._maybe_audit("read_file", {"path": "a.py"}, "content")   # not mutating → skipped
    gx10._maybe_audit("write_file", {"path": "b.py"}, "OK")       # mutating → recorded
    recs = al.read_all(tmp_path / "audit" / "ledger.jsonl")
    assert [r["payload"]["action"] for r in recs] == ["write_file"]


def test_is_audit_path_guards_the_audit_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "state_root", lambda: tmp_path)
    assert gx10._is_audit_path(tmp_path / "audit" / "ledger.jsonl") is True
    assert gx10._is_audit_path(tmp_path / "audit") is True
    assert gx10._is_audit_path(tmp_path / "vault" / "x.md") is False


def test_write_tools_refuse_the_audit_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "state_root", lambda: tmp_path)
    monkeypatch.setattr(gx10, "_LOCAL_TOOL_BRIDGE", None)
    audit_file = tmp_path / "audit" / "ledger.jsonl"
    monkeypatch.setattr(gx10, "_resolve_exec_path", lambda pth: audit_file)
    out = gx10.run_tool("write_file", {"path": "audit/ledger.jsonl", "content": "tamper"})
    assert out.startswith("ERROR") and "audit" in out.lower()
    audit_file.parent.mkdir(parents=True, exist_ok=True)
    audit_file.write_text("real", encoding="utf-8")
    out2 = gx10.run_tool("edit_file", {"path": "audit/ledger.jsonl", "old_string": "real", "new_string": "x"})
    assert out2.startswith("ERROR") and "audit" in out2.lower()
    assert audit_file.read_text(encoding="utf-8") == "real"       # refused → unchanged


def test_unwritable_intent_refuses_before_mutating_dispatch(monkeypatch, tmp_path):
    _audit_on(monkeypatch, tmp_path, "mutating")
    called = []
    monkeypatch.setattr(gx10, "_run_tool_dispatch_impl", lambda name, args: called.append(name) or "OK")
    monkeypatch.setattr(al, "record_action", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))

    out = gx10._run_tool_dispatch("write_file", {"path": "never.txt", "content": "x"})

    assert out.startswith("ERROR: audit intent append failed")
    assert called == []


def test_unwritable_intent_blocks_bridged_server_lane(monkeypatch, tmp_path):
    _audit_on(monkeypatch, tmp_path, "mutating")
    bridged = []
    monkeypatch.setattr(gx10, "_LOCAL_TOOL_BRIDGE",
                        lambda name, args: bridged.append((name, args)) or "OK")
    monkeypatch.setattr(al, "record_action", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    out = gx10.run_tool("write_file", {"path": "never.txt", "content": "x"})
    assert out.startswith("ERROR: audit intent append failed") and bridged == []


def test_result_failure_surfaces_degraded_health_and_next_intent_recovers(monkeypatch, tmp_path):
    _audit_on(monkeypatch, tmp_path, "mutating")
    dispatched = []
    monkeypatch.setattr(gx10, "_run_tool_dispatch_impl",
                        lambda name, args: dispatched.append(name) or "OK: mutated")
    real = al.record_action
    calls = []

    def fail_then_recover(*args, **kwargs):
        phase = kwargs.get("phase")
        calls.append(phase)
        if phase == "result" and calls.count("result") == 1:
            raise OSError("result sink down")
        if phase == "intent" and gx10._AUDIT_DEGRADED and calls.count("intent") == 2:
            raise OSError("intent sink still down")
        return real(*args, **kwargs)

    monkeypatch.setattr(al, "record_action", fail_then_recover)
    first = gx10._run_tool_dispatch("write_file", {"path": "a.txt", "content": "x"})
    assert "audit health is degraded" in first and gx10._AUDIT_DEGRADED is True
    second = gx10._run_tool_dispatch("write_file", {"path": "b.txt", "content": "x"})
    assert second.startswith("ERROR: audit intent append failed")
    assert "audit health is degraded" in second and gx10._AUDIT_DEGRADED is True
    assert dispatched == ["write_file"]
    third = gx10._run_tool_dispatch("write_file", {"path": "c.txt", "content": "x"})
    assert third == "OK: mutated" and gx10._AUDIT_DEGRADED is False


def test_audit_tombstones_cannot_disable_and_scope_is_validated(monkeypatch, capsys):
    cfg = gx10._code_defaults()
    cfg["audit"]["enabled"] = False
    gx10._apply_config(cfg)
    gx10._apply_config(cfg)
    warnings = [line for line in capsys.readouterr().out.splitlines() if "DEPRECATED" in line]
    assert len(warnings) == 1 and "audit.enabled" in warnings[0]
    assert "enabled" not in cfg["audit"]

    monkeypatch.setenv("GX10_AUDIT_ENABLED", "0")
    gx10._apply_env(cfg)
    assert "retired and ignored" in capsys.readouterr().out

    cfg["audit"]["scope"] = "invalid"
    with pytest.raises(ValueError, match="audit.scope"):
        gx10._apply_config(cfg)


def test_runtime_set_refuses_retired_audit_switch(monkeypatch):
    cfg = gx10._code_defaults()
    monkeypatch.setattr(gx10, "_EFFECTIVE_CFG", cfg)
    surfaced = []
    monkeypatch.setattr(gx10, "_ui_print", lambda message, *a, **k: surfaced.append(str(message)))
    gx10._dispatch(None, "config set audit.enabled false")
    assert len(surfaced) == 1 and "retired and cannot be set" in surfaced[0]
