"""Audit identity + manifest SSOT (skills/mpr/audit.py) — Spec 07 §2/§3 / §11 (a,e partial).

Deterministic, no I/O: run_id format, canonical_prompt + stable prompt_hash (replay byte-equality),
the manifest carries every required top-key, perspective entries are complete, the schema is closed,
and the manifest round-trips losslessly (pydantic-v2).
"""
from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from mpr.audit import (
    Manifest,
    PerspectiveEntry,
    Provenance,
    Query,
    RouterDecisionSnapshot,
    build_provenance,
    canonical_prompt,
    compute_status,
    content_hash,
    new_run_id,
    now_iso,
    prompt_hash,
    record_perspective,
    write_manifest,
    write_synthesis,
)


def _writer(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _persp(role="SRE/Ops", provider="spark-vllm", substrate="in-engine",
           policy="local-only", **over):
    p = {"role": role, "lens_prompt": "lens", "effort": "high", "provider": provider,
         "model": "qwen", "substrate": substrate, "provider_policy": policy,
         "rendered": {"system": "S", "user": "U"}, "context_sources": [], "max_tokens": 1536,
         "cost": {"amount": 0.0}}
    p.update(over)
    return p


def _result(ok=True, content="Roh-Gutachten Text", error=None, ctok=870, lat=6.4):
    return {"ok": ok, "content": content, "error": error, "completion_tokens": ctok, "latency": lat}

_REQUIRED_TOP = {"schema_version", "run_id", "created_at", "query", "router_decision",
                 "perspectives", "provenance", "synthesis", "final_answer", "inputs", "metrics"}


# ── §2 identity ───────────────────────────────────────────────────────────────────────────────────
def test_new_run_id_format():
    assert re.fullmatch(r"mpr-\d{8}T\d{6}Z-[0-9a-f]{8}", new_run_id())


def test_now_iso_format():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", now_iso())


def test_canonical_prompt_system_then_user():
    assert canonical_prompt({"system": "S", "user": "U"}) == "S\n\nU"
    assert canonical_prompt({"system": None, "user": "U"}) == "U"      # no system → user only
    assert canonical_prompt({"system": "", "user": "U"}) == "U"


def test_prompt_hash_stable_and_sensitive():
    a = canonical_prompt({"system": "S", "user": "U"})
    assert prompt_hash(a) == prompt_hash(a)                            # stable
    assert prompt_hash(a).startswith("sha256:")
    assert prompt_hash(canonical_prompt({"system": "S", "user": "U2"})) != prompt_hash(a)  # user change
    assert prompt_hash(canonical_prompt({"system": "S2", "user": "U"})) != prompt_hash(a)  # system change


def test_content_hash_is_sha256():
    assert content_hash("payload").startswith("sha256:")
    assert content_hash("a") != content_hash("b")


# ── §3 manifest schema ────────────────────────────────────────────────────────────────────────────
def _minimal_manifest(**over) -> Manifest:
    data = {
        "run_id": "mpr-20260619T000000Z-deadbeef",
        "created_at": "2026-06-19T00:00:00Z",
        "query": Query(text="Soll X?"),
        "router_decision": RouterDecisionSnapshot(decision="run", domain="adhoc", mode="decision"),
        "provenance": Provenance(sovereignty_ok=True),
    }
    data.update(over)
    return Manifest(**data)


def test_manifest_has_all_required_sections():
    m = _minimal_manifest()
    dumped = m.model_dump()
    assert _REQUIRED_TOP.issubset(dumped.keys())
    assert dumped["schema_version"] == "1" and dumped["status"] == "ok"
    assert dumped["audit_level"] == "manifest-only"


def test_perspective_entry_fields_complete():
    p = PerspectiveEntry(index=1, role="SRE/Ops", lens_prompt="…", provider="spark-vllm",
                         substrate="in-engine", provider_policy="local-only",
                         prompt_hash="sha256:abc", ok=True)
    d = p.model_dump()
    for key in ("role", "provider", "model", "effort", "prompt_hash", "context_sources",
                "tokens", "latency_s", "cost"):
        assert key in d
    assert d["tokens"]["prompt_estimated"] is True   # prompt never from usage (§3)


def test_manifest_rejects_extra_top_key():
    with pytest.raises(ValidationError):
        _minimal_manifest(bogus=1)


def test_manifest_roundtrip_lossless():
    m = _minimal_manifest(
        perspectives=[PerspectiveEntry(index=1, role="R", lens_prompt="l", provider="spark-vllm",
                                       substrate="in-engine", provider_policy="local-only",
                                       prompt_hash="sha256:1", ok=True)],
        final_answer="Antwort.",
    )
    again = Manifest.model_validate_json(m.model_dump_json())
    assert again == m                                 # pydantic-v2 lossless round-trip
    assert again.schema_version == "1"


def test_provenance_sovereignty_field():
    p = Provenance(sovereignty_ok=False, violations=[{"perspective_index": 2, "reason": "leak"}])
    assert p.sovereignty_ok is False and len(p.violations) == 1


# ── §8 recorder / spiegelung ─────────────────────────────────────────────────────────────────────
def test_record_in_engine_result_mirrored_manifest_only(tmp_path):
    e = record_perspective(tmp_path, 1, _persp(), _result(), "manifest-only", writer=_writer)
    assert e.index == 1 and e.ok is True and e.role == "SRE/Ops"
    assert e.prompt_hash.startswith("sha256:")
    assert e.tokens.completion == 870 and e.tokens.prompt_estimated is True
    assert e.artifact is None                      # manifest-only writes no md
    assert not list(tmp_path.glob("*.md"))


def test_record_full_writes_perspective_md(tmp_path):
    e = record_perspective(tmp_path, 2, _persp(role="Security"), _result(content="Geheim-Analyse"),
                           "full-per-perspective", writer=_writer)
    assert e.artifact == "perspective_02.md"
    md = (tmp_path / "perspective_02.md").read_text(encoding="utf-8")
    assert "Geheim-Analyse" in md and "prompt_hash:" in md and "role:Security" in md


def test_record_failed_perspective(tmp_path):
    e = record_perspective(tmp_path, 1, _persp(), _result(ok=False, content=None, error="timeout"),
                           "full-per-perspective", writer=_writer)
    assert e.ok is False and e.error == "timeout" and e.artifact is None  # no content → no md


# ── §4 sovereignty / provenance (central) ────────────────────────────────────────────────────────
def test_local_only_never_egress():
    prov = build_provenance([{"index": 1, "substrate": "in-engine", "provider": "spark-vllm",
                              "provider_policy": "local-only", "payload": "interner code"}])
    assert prov.egress == [] and prov.sovereignty_ok is True


def test_offloadable_external_is_allowed_egress():
    prov = build_provenance([{"index": 2, "substrate": "pc-cli", "provider": "claude-cli",
                              "provider_policy": "offloadable", "payload": "öffentliche frage",
                              "data_classification": "public"}])
    assert len(prov.egress) == 1 and prov.egress[0].policy_allowed is True
    assert prov.violations == [] and prov.sovereignty_ok is True


def test_local_only_external_is_violation():
    prov = build_provenance([{"index": 3, "substrate": "pc-cli", "provider": "claude-cli",
                              "provider_policy": "local-only", "payload": "sensibler code"}])
    assert prov.sovereignty_ok is False and len(prov.violations) == 1
    assert prov.egress[0].policy_allowed is False
    assert prov.violations[0]["perspective_index"] == 3


def test_egress_payload_is_hashed_not_plaintext():
    prov = build_provenance([{"index": 1, "substrate": "pc-cli", "provider": "x",
                              "provider_policy": "offloadable", "payload": "GEHEIMNIS-12345"}])
    dumped = prov.model_dump_json()
    assert prov.egress[0].payload_hash.startswith("sha256:")
    assert "GEHEIMNIS-12345" not in dumped       # raw payload never in the manifest
    assert prov.egress[0].bytes_out == len("GEHEIMNIS-12345".encode("utf-8"))


def test_sovereignty_ok_equals_no_violations():
    clean = build_provenance([{"index": 1, "substrate": "in-engine", "provider": "spark-vllm",
                               "provider_policy": "local-only", "payload": "x"}])
    leak = build_provenance([{"index": 1, "substrate": "pc-cli", "provider": "x",
                              "provider_policy": "local-only", "payload": "x"}])
    assert clean.sovereignty_ok == (clean.violations == [])
    assert leak.sovereignty_ok == (leak.violations == [])


# ── §4 status ────────────────────────────────────────────────────────────────────────────────────
def _entry(ok=True):
    return PerspectiveEntry(index=1, role="R", lens_prompt="l", provider="spark-vllm",
                            substrate="in-engine", provider_policy="local-only",
                            prompt_hash="sha256:1", ok=ok)


def test_compute_status_matrix():
    clean = Provenance(sovereignty_ok=True)
    leak = Provenance(sovereignty_ok=False, violations=[{"x": 1}])
    assert compute_status([_entry(True)], clean) == "ok"
    assert compute_status([_entry(True), _entry(False)], clean) == "partial"
    assert compute_status([_entry(True)], clean, declined=True) == "declined"
    assert compute_status([_entry(True)], leak) == "error"            # violation → error
    assert compute_status([_entry(True)], clean, write_error=True) == "error"


# ── §5.1 file-first writers ──────────────────────────────────────────────────────────────────────
def test_write_synthesis_and_manifest_commit(tmp_path):
    write_synthesis(tmp_path, "Synthese-Text", run_id="r1", template="decision-matrix",
                    conflicts=["Datenkonsistenz"], writer=_writer)
    m = _minimal_manifest(run_id="r1")
    write_manifest(tmp_path, m, writer=_writer)
    syn = (tmp_path / "synthesis.md").read_text(encoding="utf-8")
    assert "Synthese-Text" in syn and "template:decision-matrix" in syn
    reparsed = Manifest.model_validate_json((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert reparsed == m                           # manifest.json is the commit point, lossless


def test_substrate_tag_mismatch_is_violation():
    # HIGH-1: substrate=in-engine but provider not a known local backend → violation + egress recorded.
    prov = build_provenance([{"index": 1, "substrate": "in-engine", "provider": "claude-cli",
                              "provider_policy": "local-only", "payload": "leaked"}])
    assert prov.sovereignty_ok is False
    assert any("tag mismatch" in v["reason"] for v in prov.violations)
    assert len(prov.egress) == 1                     # data to a non-local backend is still egress


def test_custom_local_allowlist_respected():
    prov = build_provenance([{"index": 1, "substrate": "in-engine", "provider": "my-local",
                              "provider_policy": "local-only", "payload": "x"}],
                            local_providers={"my-local"})
    assert prov.sovereignty_ok is True and prov.egress == []   # provider now whitelisted as local


def test_non_str_payload_no_crash():
    # MED-1: a structured payload must not crash the proof.
    prov = build_provenance([{"index": 1, "substrate": "pc-cli", "provider": "x",
                              "provider_policy": "offloadable", "payload": {"a": 1}}])
    assert prov.egress[0].payload_hash.startswith("sha256:") and prov.egress[0].bytes_out > 0


def test_falsy_payload_distinct_from_missing():
    # MED-2: payload=0 must not be hashed as the empty string.
    zero = build_provenance([{"index": 1, "substrate": "pc-cli", "provider": "x",
                              "provider_policy": "offloadable", "payload": 0}])
    assert zero.egress[0].payload_hash != content_hash("") and zero.egress[0].bytes_out == 1


# ── §5.2 TaskStore index ─────────────────────────────────────────────────────────────────────────
class _FakeStore:
    def __init__(self):
        self.created = []
        self.transitions = []
        self._n = 0

    def create(self, fields, *, force=False, now_iso=None):
        self._n += 1
        tid = f"KGC-{self._n:03d}"
        self.created.append((dict(fields), force))
        return {**fields, "id": tid, "status": "pending"}

    def transition(self, task_id, to_status):
        self.transitions.append((task_id, to_status))
        return {"id": task_id, "status": to_status}


def test_run_indexed_as_task():
    from mpr.audit import index_in_taskstore
    store = _FakeStore()
    tid = index_in_taskstore("mpr-r1", "Soll X auf Postgres?", "architecture-decision", "ok", store=store)
    assert tid == "KGC-001"
    fields, force = store.created[0]
    assert force is True and fields["type"] == "mpr-run" and fields["mpr_run_id"] == "mpr-r1"
    assert fields["manifest_path"] == "runs/mpr-r1/manifest.json"   # B3: initiative-relativ (runs/<id>/)
    assert store.transitions == [("KGC-001", "done")]


def test_two_runs_same_query_both_indexed():
    from mpr.audit import index_in_taskstore
    store = _FakeStore()
    a = index_in_taskstore("mpr-a", "gleiche Frage", "d", "ok", store=store)
    b = index_in_taskstore("mpr-b", "gleiche Frage", "d", "ok", store=store)
    assert a != b and len(store.created) == 2          # force=True → no Jaccard dedup


def test_index_store_none_returns_none():
    from mpr.audit import index_in_taskstore
    assert index_in_taskstore("mpr-r", "q", "d", "ok", store=None) is None


# (§6 memory write-back is covered by synthesis.write_back's own tests; the dead duplicate
#  `mirror_to_memory` and its tests were removed in #503 MPR-3.)


# ── §9 retention ─────────────────────────────────────────────────────────────────────────────────
def _make_run(root, name, created_at, *, violations=False):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    m = {"run_id": name, "created_at": created_at,
         "provenance": {"violations": [{"x": 1}] if violations else []},
         "task_id": f"KGC-{name[-1]}"}
    (d / "manifest.json").write_text(__import__("json").dumps(m), encoding="utf-8")
    return d


def test_prune_lru_keep_runs(tmp_path):
    from mpr.audit import prune_runs
    for i in range(5):
        _make_run(tmp_path, f"mpr-2026010{i}T000000Z-aaaaaaa{i}", f"2026-01-0{i}T00:00:00Z")
    deleted = prune_runs(tmp_path, keep_runs=3, keep_days=None)
    assert len(deleted) == 2                            # 2 oldest removed, 3 newest kept
    assert not (tmp_path / "mpr-20260100T000000Z-aaaaaaa0").exists()
    assert (tmp_path / "mpr-20260104T000000Z-aaaaaaa4").exists()


def test_prune_idempotent(tmp_path):
    from mpr.audit import prune_runs
    for i in range(4):
        _make_run(tmp_path, f"mpr-2026010{i}T000000Z-bbbbbbb{i}", f"2026-01-0{i}T00:00:00Z")
    first = prune_runs(tmp_path, keep_runs=2, keep_days=None)
    second = prune_runs(tmp_path, keep_runs=2, keep_days=None)
    assert len(first) == 2 and second == []            # second pass deletes nothing


def test_violation_protected_from_prune(tmp_path):
    from mpr.audit import prune_runs
    _make_run(tmp_path, "mpr-20260101T000000Z-violated0", "2026-01-01T00:00:00Z", violations=True)
    _make_run(tmp_path, "mpr-20260102T000000Z-clean0001", "2026-01-02T00:00:00Z")
    _make_run(tmp_path, "mpr-20260103T000000Z-clean0002", "2026-01-03T00:00:00Z")
    prune_runs(tmp_path, keep_runs=1, keep_days=None)
    assert (tmp_path / "mpr-20260101T000000Z-violated0").exists()   # violation survives rotation


def test_prune_keep_days(tmp_path):
    from mpr.audit import prune_runs
    _make_run(tmp_path, "mpr-20260101T000000Z-old00000", "2026-01-01T00:00:00Z")
    _make_run(tmp_path, "mpr-20260610T000000Z-new00000", "2026-06-10T00:00:00Z")
    deleted = prune_runs(tmp_path, keep_runs=500, keep_days=30, now="2026-06-19T00:00:00Z")
    assert deleted == ["mpr-20260101T000000Z-old00000"]            # >30 days old removed


def test_prune_deletes_taskstore_entry(tmp_path):
    from mpr.audit import prune_runs
    _make_run(tmp_path, "mpr-20260101T000000Z-x0000000", "2026-01-01T00:00:00Z")
    deleted_ids = []
    prune_runs(tmp_path, keep_runs=0, keep_days=None, store_delete=deleted_ids.append)
    assert deleted_ids == ["KGC-0"]                                 # store entry removed too
