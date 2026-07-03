"""#1084 (epic #1043 quick-win): the per-action, tamper-evident audit ledger — the minimal first step of the
audit-log epic (#1067). A core-owned hash-chain ledger (not the private dev-process one) records the
orchestrator's mutating tool actions (content-free) when `audit.enabled`; default-off (byte-identical)."""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import audit_ledger as al  # noqa: E402


def test_append_chains_and_verifies(tmp_path):
    p = tmp_path / "ledger.jsonl"
    al.append(p, {"action": "write_file", "detail": "a.py"})
    al.append(p, {"action": "execute_command", "detail": "ls"})
    recs = al.read_all(p)
    assert len(recs) == 2 and recs[0]["seq"] == 0 and recs[1]["prev_hash"] == recs[0]["hash"]
    assert al.verify_chain(p) == []


def test_tampering_a_payload_is_detected(tmp_path):
    p = tmp_path / "ledger.jsonl"
    al.append(p, {"action": "write_file", "detail": "a.py"})
    al.append(p, {"action": "write_file", "detail": "b.py"})
    lines = p.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace("a.py", "evil.py")               # edit a payload without re-hashing
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert any("hash mismatch" in e for e in al.verify_chain(p))


def test_deleting_a_middle_record_is_detected(tmp_path):
    p = tmp_path / "ledger.jsonl"
    for i in range(3):
        al.append(p, {"action": "x", "detail": str(i)})
    lines = p.read_text(encoding="utf-8").splitlines()
    p.write_text(lines[0] + "\n" + lines[2] + "\n", encoding="utf-8")   # drop the middle record
    assert al.verify_chain(p)                                    # seq gap + prev_hash break


def test_record_action_is_content_free_and_bounded(tmp_path):
    p = tmp_path / "ledger.jsonl"
    rec = al.record_action(p, "execute_command", "rm -rf x", ok=False, ts=123.0)
    pay = rec["payload"]
    assert pay["action"] == "execute_command" and pay["ok"] is False and pay["ts"] == 123.0
    assert pay["actor"] == "orchestrator" and al.verify_chain(p) == []
    big = al.record_action(p, "write_file", "x" * 1000, ok=True, ts=1.0)
    assert len(big["payload"]["detail"]) == 512                 # truncated → can't bloat the trail


def test_maybe_audit_records_only_mutating_tools_when_enabled(monkeypatch, tmp_path):
    import gx10
    monkeypatch.setattr(gx10, "AUDIT_ENABLED", True)
    monkeypatch.setattr(gx10, "state_root", lambda: tmp_path)
    gx10._maybe_audit("write_file", {"path": "a.py"}, "OK: Written 3 chars to a.py")
    gx10._maybe_audit("execute_command", {"command": "ls"}, "some output")
    gx10._maybe_audit("query_memory", {"query": "x"}, "hits")   # not a mutating tool → NOT audited
    recs = al.read_all(tmp_path / "audit" / "ledger.jsonl")
    assert [r["payload"]["action"] for r in recs] == ["write_file", "execute_command"]
    assert recs[0]["payload"]["detail"] == "a.py" and recs[0]["payload"]["ok"] is True
    assert al.verify_chain(tmp_path / "audit" / "ledger.jsonl") == []


def test_maybe_audit_marks_errors_and_is_noop_when_disabled(monkeypatch, tmp_path):
    import gx10
    monkeypatch.setattr(gx10, "state_root", lambda: tmp_path)
    monkeypatch.setattr(gx10, "AUDIT_ENABLED", True)
    gx10._maybe_audit("execute_command", {"command": "boom"}, "ERROR: blocked")
    recs = al.read_all(tmp_path / "audit" / "ledger.jsonl")
    assert recs[-1]["payload"]["ok"] is False                   # a failed action is recorded as ok=False
    monkeypatch.setattr(gx10, "AUDIT_ENABLED", False)
    before = len(recs)
    gx10._maybe_audit("write_file", {"path": "z"}, "OK")        # disabled → no new record (byte-identical)
    assert len(al.read_all(tmp_path / "audit" / "ledger.jsonl")) == before
