"""#1067 (epic #1065): the FULL, immutable, tamper-evident per-action audit log. Extends #1084's
audit_ledger — `audit.scope: all` records EVERY tool call (not just the mutating subset), each record carries
who/what/when/why (actor + action + target + reason + ts), and the agent's own write tools REFUSE the audit
directory so it can't tamper with its own trail (tamper-RESISTANCE beyond the hash-chain's tamper-EVIDENCE)."""
from __future__ import annotations

import sys
import types
from pathlib import Path

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
    monkeypatch.setattr(gx10, "AUDIT_ENABLED", True)
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
