"""#1084: per-action audit ledger — the minimal first step of the audit-log epic (#1067).

An append-only, **tamper-evident** record of the orchestrator's mutating tool actions (write_file / edit_file
/ execute_command). Each record carries a hash chain (``hash = sha256(seq | prev_hash | canonical(payload))``,
the same proven scheme the dev-process transition ledger uses), so any edit, reorder, or truncation of the
audit trail is detectable by :func:`verify_chain` — even though the file itself is writable. Core-owned +
stdlib-only (the dev-process ledger is private substrate this must not import); pure/deterministic apart from
the caller-supplied timestamp. Default-off at the call site (opt-in `audit.enabled`).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

_GENESIS = "GENESIS"
_SCHEMA_VERSION = "1.0"


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(seq: int, prev_hash: str, payload: Any) -> str:
    return hashlib.sha256(f"{seq}|{prev_hash}|{_canonical(payload)}".encode("utf-8")).hexdigest()


def read_all(path: "str | Path") -> "List[Dict[str, Any]]":
    p = Path(path)
    if not p.is_file():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def append(path: "str | Path", payload: dict) -> dict:
    """Append one hash-chained record linking it to the previous (append-only integrity). Returns it."""
    p = Path(path)
    records = read_all(p)
    seq = len(records)
    prev_hash = records[-1]["hash"] if records else _GENESIS
    payload = {**payload}
    payload.setdefault("schema_version", _SCHEMA_VERSION)
    rec = {"seq": seq, "prev_hash": prev_hash, "payload": payload, "hash": _hash(seq, prev_hash, payload)}
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(_canonical(rec) + "\n")
    return rec


def verify_chain(path: "str | Path") -> "List[str]":
    """Return integrity violations ([] = intact): seq gaps, broken prev_hash links, tampered payloads."""
    errors: "List[str]" = []
    prev = _GENESIS
    for i, rec in enumerate(read_all(path)):
        if rec.get("seq") != i:
            errors.append(f"record {i}: seq {rec.get('seq')!r} (expected {i})")
        if rec.get("prev_hash") != prev:
            errors.append(f"record {i}: prev_hash break (chain reordered/truncated)")
        if _hash(rec.get("seq"), rec.get("prev_hash"), rec.get("payload")) != rec.get("hash"):
            errors.append(f"record {i}: hash mismatch (payload tampered)")
        prev = rec.get("hash")
    return errors


def record_action(path: "str | Path", action: str, detail: str, *, ok: bool, ts: float,
                  actor: str = "orchestrator", reason: str = "") -> dict:
    """Append a per-action audit record capturing WHO (*actor*) did WHAT (*action* on *detail*), WHEN (*ts*),
    WHY (*reason* — the context the action served), and whether it succeeded (*ok*). *detail* / *reason* are
    truncated so a huge argument can't bloat the trail. Never the file/command CONTENT (an audit trail records
    the action, not the payload)."""
    return append(path, {"actor": actor, "action": action, "detail": (detail or "")[:512],
                         "reason": (reason or "")[:200], "ok": bool(ok), "ts": float(ts)})
